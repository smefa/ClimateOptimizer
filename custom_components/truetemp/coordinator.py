"""DataUpdateCoordinator for TrueTemp.

Reads indoor/outdoor temperature, an optional weather forecast (wind and cloud
only), sun geometry and an optional day-ahead price; advances the offset
learner and the lag estimator; and composes the published compensated outdoor
temperature via the pure `heuristic.compute()`.

## The one timing rule that matters

The learner advances on a FIXED cadence and nothing else. Home Assistant
refreshes this coordinator both on its polling interval and whenever a watched
source entity changes state, and the second kind can fire many times a minute.
The 2026-08-07 validation traced its single largest data-quality failure to
exactly that: an estimator stepped on every event-driven refresh saw a median
6.6-minute interval against a 0.1 degC sensor resolution, which left 63% of its
transitions reading exactly zero change.

So `_async_update_data` always recomposes and republishes the output — a source
recovering should be reflected immediately — but only calls into `learner.step`
and `lag.push` once `LEARNER_STEP_SECONDS` have genuinely elapsed.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfPower,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import (
    PowerConverter,
    SpeedConverter,
    TemperatureConverter,
)

from .const import (
    CONF_COMFORT_MIN_C,
    CONF_ENABLE_DATA_LOGGING,
    CONF_ENABLE_PRICE_COMPENSATION,
    CONF_ENABLE_SOLAR_INPUT,
    CONF_ENABLE_WIND_INPUT,
    CONF_HEAT_CURVE_OFFSET_ENTITY,
    CONF_HEAT_CURVE_OFFSET_INVERT,
    CONF_HEATING_TYPE,
    CONF_INDOOR_TARGET_TEMPERATURE,
    CONF_INDOOR_TEMP_SENSOR,
    CONF_NORDPOOL_PRICE_ENTITY,
    CONF_OHMONWIFI_HOST,
    CONF_OUTDOOR_TEMP_SENSOR,
    CONF_OUTPUT_MODE,
    CONF_OUTPUT_NUMBER_ENTITY,
    CONF_POWER_SENSOR,
    CONF_WEATHER_ENTITY,
    DEFAULT_COMFORT_MIN_C,
    DEFAULT_ENABLE_DATA_LOGGING,
    DEFAULT_ENABLE_PRICE_COMPENSATION,
    DEFAULT_ENABLE_SOLAR_INPUT,
    DEFAULT_ENABLE_WIND_INPUT,
    DEFAULT_HEAT_CURVE_OFFSET_INVERT,
    DEFAULT_INDOOR_TARGET_TEMPERATURE,
    DEFAULT_OUTPUT_MODE,
    DOMAIN,
    HEATING_CUTOFF_MARGIN_C,
    LEARNER_STEP_SECONDS,
    OUTPUT_MODE_HEAT_CURVE_OFFSET,
    UPDATE_INTERVAL_MINUTES,
)
from .data_logger import async_log_record, log_file_path
from .heuristic import (
    COLD_CAUTION_MID,
    PRICE_TIER_MID,
    SOLAR_GAIN_C,
    HeuristicInputs,
    HeuristicParams,
    HeuristicResult,
    compute,
    heat_curve_offset_c,
    solar_effect_of,
)
from .lag import (
    DEFAULT_HEATING_TYPE,
    LagResult,
    LagState,
    estimate as lag_estimate,
    fallback_minutes,
    initial_state as lag_initial_state,
    push as lag_push,
)
from .learner import (
    SPOOF_PER_INDOOR_C,
    TAU_I_LAG_MULTIPLE,
    LearnerInputs,
    LearnerResult,
    LearnerState,
    initial_state as learner_initial_state,
    recoverable_sag_c,
    step as learner_step,
)
from .learner_store import (
    STORAGE_VERSION as LEARNER_STORAGE_VERSION,
    deserialize as learner_deserialize,
    serialize as learner_serialize,
    store_key as learner_store_key,
)

_LOGGER = logging.getLogger(__name__)

# Debounce window for persisting learned state. `Store.async_delay_save`
# coalesces every request inside this window into one disk write and always
# flushes on Home Assistant shutdown. The real purpose is to collapse the burst
# of extra refreshes a recovering source can trigger within a few seconds.
LEARNER_STATE_SAVE_DELAY_SECONDS = 30.0

# Skip re-pushing to an output target when the value has not meaningfully moved
# since that channel's last successful push, so a real hardware register is not
# rewritten every cycle for sub-noise changes.
OUTPUT_WRITE_TOLERANCE_C = 0.05

# Timeout for the direct OhmOnWifi local-API push. A plain HTTP GET to a device
# on the local network, so a short timeout is right — no point blocking a whole
# cycle on a device that has gone unreachable.
OHMONWIFI_REQUEST_TIMEOUT_SECONDS = 10.0

# Tolerance on the learner cadence. HA's scheduler jitters by a second or two,
# and demanding the full interval would drop roughly every other step.
LEARNER_STEP_TOLERANCE = 0.9


def _entry_value(entry: ConfigEntry, key: str, default):
    """Options override data, both fall back to `default`."""
    return entry.options.get(key, entry.data.get(key, default))


def _state_is_usable(state: State | None) -> bool:
    return state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)


def _as_float(state: State, attribute: str | None = None) -> float | None:
    raw = state.attributes.get(attribute) if attribute else state.state
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class TrueTempCoordinator(DataUpdateCoordinator[HeuristicResult]):
    """Fetches inputs each cycle, advances learning on a fixed clock, and
    composes the published result."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )

        # --- Learned state ---------------------------------------------------
        # The offset table and the lag buffer are the only things this
        # integration learns. Both are persisted; neither is ever invalidated
        # by a configuration change, unlike the estimator state this replaced.
        self.learner_state: LearnerState = learner_initial_state()
        self.lag_state: LagState = lag_initial_state(UPDATE_INTERVAL_MINUTES)
        self.learner_result: LearnerResult | None = None
        self.lag_result: LagResult | None = None
        self._learner_last_step_s: float | None = None
        # Whether the learner advanced on the most recent cycle. Recorded in
        # the log so an offline replay can separate decision cycles from bare
        # republishes — see `_build_log_record`.
        self._learner_stepped: bool = False
        self._store: Store[dict] = Store(
            hass, LEARNER_STORAGE_VERSION, learner_store_key(entry.entry_id)
        )

        # --- Optional power draw (diagnostic echo + local-log cost) ----------
        # Purely informational: never read by the controller. The monotonic
        # timer only advances when a cycle is actually logged.
        self.last_power_w: float | None = None
        self.last_power_data_available: bool = False
        self._energy_last_monotonic: float | None = None

        # --- Error tracking ----------------------------------------------------
        # HA's DataUpdateCoordinator.last_exception is sticky - it is set on
        # failure but never cleared on a later successful update - so reading
        # it directly for a "last error" display would show a stale message
        # forever after the sensor recovers. Track our own timestamp instead,
        # scoped to the exact call that can raise UpdateFailed, and clear it as
        # soon as that call succeeds again.
        self.last_error_at: datetime | None = None

        # --- Output push channels -------------------------------------------
        # Independent, and both pushed every cycle when configured. Each tracks
        # its own last-written value so an unchanged value does not keep
        # rewriting a device register, and so one channel failing never
        # suppresses a retry on the other.
        self._last_ohmonwifi_value_c: float | None = None
        self._last_output_number_value_c: float | None = None
        self._last_heat_curve_offset_value_c: int | None = None

        # --- Live control surface (restored by the entities) -----------------
        # All in-memory rather than config options: an options change reloads
        # the entry, and these get nudged often by hand, schedules and
        # automations. Each entity restores its own value via RestoreEntity;
        # the values here are only the pre-restore defaults.
        #
        # Compensation ships OFF: the occupant opts in once they trust the
        # recommendation, rather than the integration silently steering the
        # heat pump from the moment it's installed.
        self.is_active: bool = False
        self.indoor_target_c: float = _entry_value(
            entry, CONF_INDOOR_TARGET_TEMPERATURE, DEFAULT_INDOOR_TARGET_TEMPERATURE
        )
        self.price_comfort_tier: str = PRICE_TIER_MID
        self.cold_caution: str = COLD_CAUTION_MID

        # The exact price forecast this cycle's decision used, stashed for the
        # data logger — forecasts get revised, so realised prices are not a
        # substitute for what was known at decision time.
        self._last_price_forecast: tuple[tuple[float, float], ...] | None = None

    # --- Configuration views -------------------------------------------------

    def watched_entity_ids(self) -> list[str]:
        """Source entities whose state changes should trigger an immediate
        refresh instead of waiting for the poll.

        HA's DataUpdateCoordinator skips notifying entities on consecutive
        identical failures, so there is no external visibility into whether its
        background timer is still quietly retrying. A real incident had this
        integration stuck unavailable for six minutes after its (cloud-polled,
        slow to reconnect) source had already recovered. Reacting to the
        sources' own state changes fixes that.

        Note this only republishes: learning still advances on its own clock.
        """
        keys = (
            CONF_OUTDOOR_TEMP_SENSOR,
            CONF_INDOOR_TEMP_SENSOR,
            CONF_NORDPOOL_PRICE_ENTITY,
            CONF_POWER_SENSOR,
        )
        return [
            entity_id
            for entity_id in (_entry_value(self.entry, key, None) for key in keys)
            if entity_id
        ]

    @property
    def price_configured(self) -> bool:
        return bool(_entry_value(self.entry, CONF_NORDPOOL_PRICE_ENTITY, None))

    @property
    def price_enabled(self) -> bool:
        return bool(
            _entry_value(
                self.entry,
                CONF_ENABLE_PRICE_COMPENSATION,
                DEFAULT_ENABLE_PRICE_COMPENSATION,
            )
        )

    @property
    def output_mode(self) -> str:
        return _entry_value(self.entry, CONF_OUTPUT_MODE, DEFAULT_OUTPUT_MODE)

    @property
    def output_number_entity_id(self) -> str | None:
        return _entry_value(self.entry, CONF_OUTPUT_NUMBER_ENTITY, None) or None

    @property
    def ohmonwifi_host(self) -> str | None:
        return _entry_value(self.entry, CONF_OHMONWIFI_HOST, None) or None

    @property
    def heat_curve_offset_entity_id(self) -> str | None:
        return _entry_value(self.entry, CONF_HEAT_CURVE_OFFSET_ENTITY, None) or None

    @property
    def heat_curve_offset_invert(self) -> bool:
        return bool(
            _entry_value(
                self.entry,
                CONF_HEAT_CURVE_OFFSET_INVERT,
                DEFAULT_HEAT_CURVE_OFFSET_INVERT,
            )
        )

    @property
    def data_logging_enabled(self) -> bool:
        return bool(
            _entry_value(
                self.entry, CONF_ENABLE_DATA_LOGGING, DEFAULT_ENABLE_DATA_LOGGING
            )
        )

    @property
    def data_log_path(self) -> str | None:
        if not self.data_logging_enabled:
            return None
        return str(log_file_path(self.hass, self.entry.entry_id))

    @property
    def solar_input_enabled(self) -> bool:
        return bool(
            _entry_value(
                self.entry, CONF_ENABLE_SOLAR_INPUT, DEFAULT_ENABLE_SOLAR_INPUT
            )
        )

    @property
    def wind_input_enabled(self) -> bool:
        return bool(
            _entry_value(self.entry, CONF_ENABLE_WIND_INPUT, DEFAULT_ENABLE_WIND_INPUT)
        )

    @property
    def heating_type(self) -> str:
        return _entry_value(self.entry, CONF_HEATING_TYPE, DEFAULT_HEATING_TYPE)

    @property
    def weather_needed(self) -> bool:
        """Whether the optional weather entity is worth reading at all. Its only
        consumers are the wind and solar terms, so with both off the per-cycle
        forecast service calls are skipped entirely."""
        return self.solar_input_enabled or self.wind_input_enabled

    @property
    def heating_cutoff_c(self) -> float:
        """Outdoor temperature at or above which compensation is suppressed.

        Derived from the target rather than configured: heating is not wanted
        once it is nearly as warm outside as the temperature being held inside,
        and the margin is what stops a cold indoor reading or a windy afternoon
        from calling for heat on a warm day.
        """
        return self.indoor_target_c - HEATING_CUTOFF_MARGIN_C

    def _params(self) -> HeuristicParams:
        """This cycle's occupant preferences. No control gains live here."""
        return HeuristicParams(
            indoor_target_c=self.indoor_target_c,
            comfort_min_c=_entry_value(
                self.entry, CONF_COMFORT_MIN_C, DEFAULT_COMFORT_MIN_C
            ),
            heating_cutoff_c=self.heating_cutoff_c,
            enable_price_compensation=self.price_enabled,
            price_comfort_tier=self.price_comfort_tier,
            cold_caution=self.cold_caution,
            enable_solar_input=self.solar_input_enabled,
            enable_wind_input=self.wind_input_enabled,
            spoof_per_indoor_c=SPOOF_PER_INDOOR_C,
        )

    # --- Source health as Repairs ---------------------------------------------
    # HA's own notification bell surfaces active Repairs issues, so this is the
    # "default trigger" for posting a real error/warning without a bespoke
    # automation. One issue per source, keyed by entry so multiple zones don't
    # collide; created while the source is down, deleted the moment it is not.

    _SOURCE_ISSUE_KEYS = (
        "outdoor_sensor_unavailable",
        "indoor_sensor_unavailable",
        "wind_forecast_unavailable",
        "cloud_sun_forecast_unavailable",
        "price_unavailable",
    )

    def _sync_source_issue(
        self,
        key: str,
        ok: bool,
        severity: ir.IssueSeverity,
        entity_id: str | None,
    ) -> None:
        issue_id = f"{key}_{self.entry.entry_id}"
        if ok:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=severity,
            translation_key=key,
            translation_placeholders={
                "name": self.entry.title,
                "entity_id": entity_id or "",
            },
        )

    def _sync_optional_source_issue(
        self, key: str, enabled: bool, ok: bool, entity_id: str | None
    ) -> None:
        """Same as `_sync_source_issue`, but for a source that can be switched
        off entirely — in which case it is never a problem."""
        self._sync_source_issue(key, ok=not enabled or ok, severity=ir.IssueSeverity.WARNING, entity_id=entity_id)

    def clear_source_issues(self) -> None:
        """Drop every issue this entry could own, e.g. on unload — otherwise a
        Repair raised while broken outlives the integration that raised it."""
        for key in self._SOURCE_ISSUE_KEYS:
            ir.async_delete_issue(self.hass, DOMAIN, f"{key}_{self.entry.entry_id}")

    # --- Source reads --------------------------------------------------------

    def _read_indoor_temp_c(self) -> tuple[float | None, bool]:
        """Return (indoor_temp_c, available).

        Soft-degrades: without an indoor reading the learner freezes and holds
        its last offset, which is a safe hold rather than a failure.
        """
        entity_id = _entry_value(self.entry, CONF_INDOOR_TEMP_SENSOR, None)
        state = self.hass.states.get(entity_id)
        if not _state_is_usable(state):
            _LOGGER.warning(
                "Indoor temperature sensor %s is unavailable; holding the learned "
                "offset and pausing learning this cycle",
                entity_id,
            )
            return None, False
        value = _as_float(state)
        if value is None:
            _LOGGER.warning("Indoor temperature sensor %s has no numeric state", entity_id)
            return None, False
        unit = state.attributes.get("unit_of_measurement", UnitOfTemperature.CELSIUS)
        return TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS), True

    def _read_raw_outdoor_temp_c(self) -> float:
        """The one hard-required source: without it there is nothing to publish
        at all, so this is the single read that still fails the update."""
        entity_id = _entry_value(self.entry, CONF_OUTDOOR_TEMP_SENSOR, None)
        state = self.hass.states.get(entity_id)
        if not _state_is_usable(state):
            raise UpdateFailed(f"Outdoor temperature sensor {entity_id} is unavailable")
        value = _as_float(state)
        if value is None:
            raise UpdateFailed(
                f"Outdoor temperature sensor {entity_id} has no numeric state"
            )
        unit = state.attributes.get("unit_of_measurement", UnitOfTemperature.CELSIUS)
        return TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)

    async def _read_forecast(self) -> tuple[float, bool, float | None, bool]:
        """Return (wind_speed_ms, wind_ok, cloud_coverage_pct, cloud_ok).

        Wind and cloud are tracked independently: not every weather integration
        provides both, and a missing wind value must not discard a perfectly
        good cloud reading. Tries hourly then daily, filling in whichever field
        is still missing. Any failure soft-degrades — the weather entity is
        enrichment only, since raw outdoor temperature comes from a dedicated
        sensor.
        """
        if not self.weather_needed:
            return 0.0, False, None, False

        weather_entity_id = _entry_value(self.entry, CONF_WEATHER_ENTITY, None)
        if not weather_entity_id:
            _LOGGER.debug("No weather entity configured; wind/solar terms contribute 0")
            return 0.0, False, None, False
        weather_state = self.hass.states.get(weather_entity_id)
        if not _state_is_usable(weather_state):
            _LOGGER.warning(
                "Weather entity %s is unavailable; continuing without wind/solar data",
                weather_entity_id,
            )
            return 0.0, False, None, False

        wind_speed_ms: float | None = None
        cloud_coverage_pct: float | None = None

        for forecast_type in ("hourly", "daily"):
            if wind_speed_ms is not None and cloud_coverage_pct is not None:
                break
            try:
                response = await self.hass.services.async_call(
                    "weather",
                    "get_forecasts",
                    {"entity_id": weather_entity_id, "type": forecast_type},
                    blocking=True,
                    return_response=True,
                )
                forecast = response[weather_entity_id]["forecast"]
                if not forecast:
                    continue
                first = forecast[0]
                if wind_speed_ms is None:
                    raw_wind = first.get("wind_speed")
                    if raw_wind is not None:
                        wind_unit = weather_state.attributes.get(
                            "wind_speed_unit", UnitOfSpeed.METERS_PER_SECOND
                        )
                        wind_speed_ms = SpeedConverter.convert(
                            float(raw_wind), wind_unit, UnitOfSpeed.METERS_PER_SECOND
                        )
                if cloud_coverage_pct is None:
                    raw_cloud = first.get("cloud_coverage")
                    if raw_cloud is not None:
                        cloud_coverage_pct = float(raw_cloud)
            except Exception as err:  # noqa: BLE001 - soft-degrade on any failure
                _LOGGER.debug(
                    "Forecast type %s unavailable for %s: %s",
                    forecast_type,
                    weather_entity_id,
                    err,
                )

        wind_ok = wind_speed_ms is not None
        cloud_ok = cloud_coverage_pct is not None
        if not wind_ok and self.wind_input_enabled:
            _LOGGER.warning(
                "Could not retrieve a wind forecast for %s; wind term contributes 0",
                weather_entity_id,
            )
        if not cloud_ok and self.solar_input_enabled:
            _LOGGER.warning(
                "Could not retrieve a cloud/sun forecast for %s; assuming clear sky",
                weather_entity_id,
            )
        return (
            wind_speed_ms if wind_ok else 0.0,
            wind_ok,
            cloud_coverage_pct,
            cloud_ok,
        )

    def _read_sun_elevation(self) -> float:
        sun_state = self.hass.states.get("sun.sun")
        if sun_state is None:
            return 0.0
        try:
            return float(sun_state.attributes.get("elevation", 0.0))
        except (TypeError, ValueError):
            return 0.0

    def _read_price(self) -> tuple[float | None, bool]:
        entity_id = _entry_value(self.entry, CONF_NORDPOOL_PRICE_ENTITY, None)
        if not entity_id:
            return None, False
        state = self.hass.states.get(entity_id)
        if not _state_is_usable(state):
            _LOGGER.warning(
                "Price entity %s is unavailable; price compensation contributes 0",
                entity_id,
            )
            return None, False
        value = _as_float(state)
        if value is None:
            _LOGGER.warning("Price entity %s has no numeric state", entity_id)
            return None, False
        return value, True

    def _read_price_entries(self) -> list[tuple[datetime, float]]:
        """Parse the price sensor's `raw_today`/`raw_tomorrow` attributes (the
        well-known Nordpool shape: lists of {start, end, value}) into
        (local start datetime, price) pairs."""
        entity_id = _entry_value(self.entry, CONF_NORDPOOL_PRICE_ENTITY, None)
        if not entity_id:
            return []
        state = self.hass.states.get(entity_id)
        if not _state_is_usable(state):
            return []
        entries: list[tuple[datetime, float]] = []
        for attr in ("raw_today", "raw_tomorrow"):
            raw = state.attributes.get(attr)
            if not isinstance(raw, (list, tuple)):
                continue
            for item in raw:
                if not isinstance(item, dict):
                    continue
                start = item.get("start")
                value = item.get("value")
                if isinstance(start, str):
                    start = dt_util.parse_datetime(start)
                if start is None or value is None:
                    continue
                try:
                    entries.append((dt_util.as_local(start), float(value)))
                except (TypeError, ValueError):
                    continue
        return entries

    def _price_forecast_offsets(self) -> tuple[tuple[float, float], ...] | None:
        """Day-ahead price as (hours_from_now, price) pairs, or None.

        None means the price logic does nothing at all this cycle — without a
        day-distribution there is no principled way to know whether the current
        price is high (see `heuristic._price_band_info`).
        """
        entries = self._read_price_entries()
        if not entries:
            return None
        now = dt_util.now()
        return tuple(
            ((start - now).total_seconds() / 3600.0, price)
            for start, price in sorted(entries, key=lambda e: e[0])
        )

    def _read_power_w(self) -> tuple[float | None, bool]:
        """Optional heat-pump power draw. Soft-degrades like price: never an
        input to the controller, only to the diagnostic echo and the log."""
        entity_id = _entry_value(self.entry, CONF_POWER_SENSOR, None)
        if not entity_id:
            return None, False
        state = self.hass.states.get(entity_id)
        if not _state_is_usable(state):
            _LOGGER.debug("Power sensor %s is unavailable this cycle", entity_id)
            return None, False
        value = _as_float(state)
        if value is None:
            _LOGGER.warning("Power sensor %s has no numeric state", entity_id)
            return None, False
        unit = state.attributes.get("unit_of_measurement", UnitOfPower.WATT)
        try:
            return PowerConverter.convert(value, unit, UnitOfPower.WATT), True
        except Exception:  # noqa: BLE001 - unrecognized unit, treat as unavailable
            _LOGGER.warning("Power sensor %s has an unrecognized unit %s", entity_id, unit)
            return None, False

    def _cycle_energy_and_cost(
        self, power_ok: bool, current_price: float | None, price_ok: bool
    ) -> tuple[float | None, float | None]:
        """Coarse energy/cost for one logged cycle: the last power reading held
        constant over the time since the previous logged cycle. Adequate for an
        offline cost trend, not billing-grade.

        On installs where the power sensor is shared with hot water production
        this is NOT attributable to space heating alone — see README.
        """
        now = time.monotonic()
        last = self._energy_last_monotonic
        self._energy_last_monotonic = now
        if not power_ok or last is None or self.last_power_w is None:
            return None, None
        dt_h = (now - last) / 3600.0
        energy_kwh = self.last_power_w / 1000.0 * dt_h
        cost = (
            energy_kwh * current_price
            if (price_ok and current_price is not None)
            else None
        )
        return energy_kwh, cost

    # --- Learning ------------------------------------------------------------

    def _lag_result(self) -> LagResult:
        """The current lag estimate, recomputed lazily and cached per step."""
        if self.lag_result is None:
            self.lag_result = lag_estimate(self.lag_state, self.heating_type)
        return self.lag_result

    def _due_for_learner_step(self) -> bool:
        """Whether enough real time has elapsed to advance learning.

        This is the guard that keeps event-driven refreshes out of the learned
        state — see the module docstring.
        """
        now = time.monotonic()
        last = self._learner_last_step_s
        if last is None:
            return True
        return (now - last) >= LEARNER_STEP_SECONDS * LEARNER_STEP_TOLERANCE

    def _advance_learning(
        self,
        indoor_temp_c: float | None,
        indoor_ok: bool,
        raw_outdoor_temp_c: float,
        sun_elevation_deg: float,
        cloud_coverage_pct: float | None,
    ) -> bool:
        """Step the lag estimator and the offset learner exactly one cycle.

        Returns whether the learner actually advanced, which the data logger
        records so an offline replay can tell decision cycles apart from bare
        republishes.

        Wrapped so a bug in either can never break the published output: on
        failure the previous result stands, which is a held offset.

        The heating-cutoff and price-braking flags the learner needs come from
        `heuristic.compute`, which needs the learner's offset — so cutoff is
        recomputed directly here (it is a one-line comparison) and price
        braking is read from the PREVIOUS cycle. One cycle of staleness on a
        15-minute clock is immaterial against lags measured in hours, and it
        avoids an ordering cycle between the two.
        """
        now = time.monotonic()
        last = self._learner_last_step_s
        dt_hours = (now - last) / 3600.0 if last is not None else 0.0
        self._learner_last_step_s = now

        # Same formula and same SOLAR_GAIN_C the feedforward term in
        # heuristic.compute() uses, threaded across the module boundary as a
        # plain float — see learner.py's module docstring and
        # HeuristicParams.spoof_per_indoor_c for why neither pure module
        # imports the other. Gated on solar_input_enabled, NOT on whether the
        # weather-derived solar_effect happens to be nonzero: a disabled
        # input means the occupant has told us the indoor sensor gets no real
        # solar gain (e.g. a basement), so the correction must be exactly 0.0
        # regardless of what the sun/cloud formula alone would say — mirrors
        # heuristic.compute()'s own `enable_solar_input` gate on
        # sun_adjustment_c.
        # `_read_sun_elevation` already falls back to 0.0 (below horizon) when
        # `sun.sun` is missing or unparsable, which makes solar_effect_of
        # return 0.0 too — a degenerate reading degrades to "no solar gain",
        # never a wild correction, so no extra guard is needed here.
        estimated_solar_gain_c = 0.0
        if self.solar_input_enabled:
            solar_effect = solar_effect_of(sun_elevation_deg, cloud_coverage_pct)
            estimated_solar_gain_c = SOLAR_GAIN_C * solar_effect

        try:
            self.lag_state = lag_push(
                self.lag_state,
                self.learner_state.prev_offset_c,
                indoor_temp_c,
                self.indoor_target_c,
            )
            self.lag_result = lag_estimate(self.lag_state, self.heating_type)

            previous = self.data
            self.learner_state, self.learner_result = learner_step(
                self.learner_state,
                LearnerInputs(
                    now_s=now,
                    dt_hours=dt_hours,
                    indoor_temp_c=indoor_temp_c,
                    indoor_data_available=indoor_ok,
                    target_c=self.indoor_target_c,
                    outdoor_temp_c=raw_outdoor_temp_c,
                    heating_cutoff_engaged=raw_outdoor_temp_c >= self.heating_cutoff_c,
                    is_active=self.is_active,
                    price_braking=bool(previous and previous.price_braking),
                    rise_hours=self.lag_result.rise_hours,
                    estimated_solar_gain_c=estimated_solar_gain_c,
                ),
            )
            _LOGGER.debug(
                "Learner: %s | Lag: %s",
                self.learner_result.reason,
                self.lag_result.reason,
            )
            self._schedule_state_save()
            return True
        except Exception as err:  # noqa: BLE001 - never break output over learning
            _LOGGER.warning("Learner step failed (holding previous offset): %s", err)
            return False

    def _schedule_state_save(self) -> None:
        try:
            self._store.async_delay_save(
                lambda: learner_serialize(self.learner_state, self.lag_state),
                LEARNER_STATE_SAVE_DELAY_SECONDS,
            )
        except Exception as err:  # noqa: BLE001 - persistence is best-effort
            _LOGGER.warning("Could not schedule learned-state save (ignored): %s", err)

    async def async_load_state(self) -> None:
        """Restore learned state before the first refresh.

        Strictly additive and self-guarded: an empty, corrupt or
        version-mismatched store is silently discarded in favour of a clean
        cold start, and any storage error is logged and swallowed.
        """
        try:
            stored = await self._store.async_load()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not load learned state (cold starting): %s", err)
            return
        if stored is None:
            return
        restored = learner_deserialize(stored)
        if restored is None:
            _LOGGER.info("Stored learned state was not usable; starting fresh")
            return
        self.learner_state, self.lag_state = restored
        _LOGGER.debug(
            "Restored learned state: %d samples, %d lag samples",
            self.learner_state.total_samples,
            len(self.lag_state.indoors_c),
        )

    async def async_save_state_now(self) -> None:
        """Flush any pending debounced write before teardown. HA only
        auto-flushes `async_delay_save` on full shutdown, not on an entry
        reload, so without this the most recent learning is lost on every
        options change."""
        try:
            await self._store.async_save(
                learner_serialize(self.learner_state, self.lag_state)
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not save learned state (ignored): %s", err)

    # --- The cycle -----------------------------------------------------------

    async def _async_update_data(self) -> HeuristicResult:
        try:
            raw_outdoor_temp_c = self._read_raw_outdoor_temp_c()
        except UpdateFailed:
            self.last_error_at = dt_util.utcnow()
            self._sync_source_issue(
                "outdoor_sensor_unavailable",
                ok=False,
                severity=ir.IssueSeverity.ERROR,
                entity_id=_entry_value(self.entry, CONF_OUTDOOR_TEMP_SENSOR, None),
            )
            raise
        self.last_error_at = None
        self._sync_source_issue(
            "outdoor_sensor_unavailable", ok=True, severity=ir.IssueSeverity.ERROR, entity_id=None
        )
        indoor_temp_c, indoor_ok = self._read_indoor_temp_c()
        wind_speed_ms, wind_ok, cloud_coverage_pct, cloud_ok = await self._read_forecast()
        sun_elevation_deg = self._read_sun_elevation()
        current_price, price_ok = self._read_price()
        self._sync_source_issue(
            "indoor_sensor_unavailable",
            ok=indoor_ok,
            severity=ir.IssueSeverity.WARNING,
            entity_id=_entry_value(self.entry, CONF_INDOOR_TEMP_SENSOR, None),
        )
        self._sync_optional_source_issue(
            "wind_forecast_unavailable",
            self.wind_input_enabled,
            wind_ok,
            _entry_value(self.entry, CONF_WEATHER_ENTITY, None),
        )
        self._sync_optional_source_issue(
            "cloud_sun_forecast_unavailable",
            self.solar_input_enabled,
            cloud_ok,
            _entry_value(self.entry, CONF_WEATHER_ENTITY, None),
        )
        self._sync_optional_source_issue(
            "price_unavailable",
            self.price_configured,
            price_ok,
            _entry_value(self.entry, CONF_NORDPOOL_PRICE_ENTITY, None),
        )
        price_forecast = self._price_forecast_offsets()
        self._last_price_forecast = price_forecast
        power_w, power_ok = self._read_power_w()
        self.last_power_w = power_w
        self.last_power_data_available = power_ok

        # Learning advances on its own clock; republishing happens every time.
        self._learner_stepped = self._due_for_learner_step() and self._advance_learning(
            indoor_temp_c,
            indoor_ok,
            raw_outdoor_temp_c,
            sun_elevation_deg,
            cloud_coverage_pct,
        )

        lag = self._lag_result()
        learner = self.learner_result
        offset_c = learner.offset_c if learner else 0.0
        # How much sag the current bin has been observed to buy back within one
        # integrator time constant — the window the loop targets anyway.
        recovery_window_h = lag.rise_hours * TAU_I_LAG_MULTIPLE
        recoverable = (
            recoverable_sag_c(learner.close_rate_c_per_h, recovery_window_h)
            if learner
            else 0.0
        )

        result = compute(
            HeuristicInputs(
                indoor_temp_c=indoor_temp_c,
                indoor_data_available=indoor_ok,
                raw_outdoor_temp_c=raw_outdoor_temp_c,
                wind_speed_ms=wind_speed_ms,
                wind_data_available=wind_ok,
                sun_elevation_deg=sun_elevation_deg,
                cloud_coverage_pct=cloud_coverage_pct,
                cloud_data_available=cloud_ok,
                current_price=current_price,
                price_data_available=price_ok,
                price_forecast=price_forecast,
                learned_offset_c=offset_c,
                fall_minutes=lag.fall_minutes,
                rise_minutes=lag.rise_minutes,
                recoverable_sag_c=recoverable,
            ),
            self._params(),
        )

        # A real side effect when configured, but it never affects `result`.
        await self._async_push_output(result)

        if self.data_logging_enabled:
            await self._log_data_point(result)

        return result

    # --- Output push ---------------------------------------------------------

    async def _async_push_output(self, result: HeuristicResult) -> None:
        """Push the published value to whichever target the selected output
        mode uses.

        The two outdoor-spoofing channels are independent, not alternatives —
        set one, both or neither. The heat-curve-offset channel is a genuine
        alternative to both of them (it writes a delta to a different pump
        parameter, not a faked outdoor reading), so it is gated by
        `output_mode` rather than by whether its own entity is configured:
        leaving stale values in the other section's fields must never cause a
        double push to the same pump.
        """
        if self.output_mode == OUTPUT_MODE_HEAT_CURVE_OFFSET:
            await self._async_push_heat_curve_offset(result)
            return

        host = self.ohmonwifi_host
        entity_id = self.output_number_entity_id
        if not host and not entity_id:
            return
        value = (
            result.raw_outdoor_temp_c
            if not self.is_active
            else result.compensated_outdoor_temp_c
        )
        if host:
            await self._async_push_ohmonwifi(host, value)
        if entity_id:
            await self._async_push_number_entity(entity_id, value)

    async def _async_push_heat_curve_offset(self, result: HeuristicResult) -> None:
        """Push a delta to the pump's own heat-curve-offset parameter instead
        of a faked outdoor reading. Zero while compensation is off — the same
        "preview without touching the pump" semantics the outdoor-spoof path
        gets by publishing the unmodified raw temperature.
        """
        entity_id = self.heat_curve_offset_entity_id
        if not entity_id:
            return
        value = (
            0
            if not self.is_active
            else heat_curve_offset_c(
                result.compensated_outdoor_temp_c,
                result.raw_outdoor_temp_c,
                self.heat_curve_offset_invert,
            )
        )
        if (
            self._last_heat_curve_offset_value_c is not None
            and abs(value - self._last_heat_curve_offset_value_c)
            < OUTPUT_WRITE_TOLERANCE_C
        ):
            return
        try:
            await self.hass.services.async_call(
                # Accepts a "number" or "input_number" entity (see the
                # `heat_curve_offset_entity` selector in config_flow.py) —
                # both expose an identical `set_value(entity_id, value)`
                # service, keyed by the entity's own domain.
                entity_id.split(".", 1)[0],
                "set_value",
                {"entity_id": entity_id, "value": value},
                blocking=True,
            )
            self._last_heat_curve_offset_value_c = value
        except Exception as err:  # noqa: BLE001 - best-effort, never break output
            _LOGGER.warning("Could not push to %s (ignored): %s", entity_id, err)

    async def _async_push_ohmonwifi(self, host: str, value: float) -> None:
        """GET `http://<host>/AT/?T=<value>` — OhmOnWifi's own local API, which
        sets its emulated resistance output so the connected heat pump reads
        `value` as the outdoor temperature. Plain HTTP, no authentication, per
        the vendor's published API doc. Best-effort: any failure is logged and
        swallowed."""
        if (
            self._last_ohmonwifi_value_c is not None
            and abs(value - self._last_ohmonwifi_value_c) < OUTPUT_WRITE_TOLERANCE_C
        ):
            return
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                f"http://{host}/AT/",
                params={"T": f"{value:.1f}"},
                timeout=aiohttp.ClientTimeout(total=OHMONWIFI_REQUEST_TIMEOUT_SECONDS),
            ) as response:
                response.raise_for_status()
            self._last_ohmonwifi_value_c = value
        except Exception as err:  # noqa: BLE001 - best-effort, never break output
            _LOGGER.warning(
                "Could not push to OhmOnWifi device %s (ignored): %s", host, err
            )

    async def _async_push_number_entity(self, entity_id: str, value: float) -> None:
        """Push via a HA `number.set_value` call. Best-effort: any failure
        (entity gone, wrong domain, outside the target's min/max) is logged and
        swallowed."""
        if (
            self._last_output_number_value_c is not None
            and abs(value - self._last_output_number_value_c) < OUTPUT_WRITE_TOLERANCE_C
        ):
            return
        try:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": entity_id, "value": value},
                blocking=True,
            )
            self._last_output_number_value_c = value
        except Exception as err:  # noqa: BLE001 - best-effort, never break output
            _LOGGER.warning("Could not push to %s (ignored): %s", entity_id, err)

    # --- Opt-in local history logging ---------------------------------------

    def _build_log_record(self, result: HeuristicResult) -> dict:
        """Flatten this cycle into one record for the local history log.

        Raw physical inputs first (what a replay needs to re-drive a candidate
        controller), then what this one actually did. The learner's internal
        state is included because it is not reconstructible from config: the
        offset moves every cycle, and without it an offline replay cannot tell
        what the controller was doing or why.

        `learner_stepped` is the field a replay must filter on. A record is
        written on every coordinator cycle, including the extra refreshes a
        recovering source triggers, but the learner only advances on its fixed
        clock — so consecutive records inside one interval repeat identical
        `learner_*` values while the raw inputs differ. Treating every line as
        a decision is exactly the irregular-cadence mistake that made the
        previous model's data unusable, so the distinction is recorded rather
        than left to be inferred from timestamps.
        """
        learner = self.learner_result
        lag = self.lag_result
        record: dict = {
            "ts": dt_util.utcnow().isoformat(),
            "entry_id": self.entry.entry_id,
            "learner_stepped": self._learner_stepped,
            # --- raw inputs ---
            "indoor_temp_c": result.indoor_temp_c,
            "indoor_ok": result.indoor_data_available,
            "raw_outdoor_temp_c": result.raw_outdoor_temp_c,
            "wind_speed_ms": result.wind_speed_ms,
            "wind_ok": result.wind_data_available,
            "cloud_coverage_pct": result.cloud_coverage_pct,
            "cloud_ok": result.cloud_data_available,
            "solar_effect": result.solar_effect,
            "current_price": result.current_price,
            "price_ok": result.price_data_available,
            "power_w": self.last_power_w,
            # --- live control surface ---
            "is_active": self.is_active,
            "output_mode": self.output_mode,
            "indoor_target_c": result.indoor_target_c,
            "effective_target_c": result.effective_indoor_target_c,
            "price_comfort_tier": result.price_comfort_tier,
            "cold_caution": result.cold_caution,
            # --- composed output ---
            "compensated_outdoor_temp_c": result.compensated_outdoor_temp_c,
            "learned_offset_c": result.learned_offset_c,
            "wind_adjustment_c": result.wind_adjustment_c,
            "sun_adjustment_c": result.sun_adjustment_c,
            "price_adjustment_c": result.price_adjustment_c,
            "price_response": result.price_response,
            "cold_brake_factor": result.cold_brake_factor,
            "price_braking": result.price_braking,
            "precharge_active": result.precharge_active,
            "heating_cutoff_engaged": result.heating_cutoff_engaged,
            "lead_minutes_effective": result.lead_minutes_effective,
        }
        if learner is not None:
            record.update(
                {
                    "learner_bin": learner.bin_index,
                    "learner_hold_offset_c": learner.hold_offset_c,
                    "learner_proportional_c": learner.proportional_c,
                    "learner_authority_c": learner.authority_c,
                    "learner_ceiling_c": learner.ceiling_c,
                    "learner_close_rate_c_per_h": learner.close_rate_c_per_h,
                    "learner_tau_i_h": learner.tau_i_h,
                    "learner_error_c": learner.error_c,
                    "learner_saturated": learner.saturated,
                    "learner_frozen": learner.frozen,
                    "learner_total_samples": learner.total_samples,
                    "learner_coverage": learner.coverage,
                    # Open-loop baseline (see learner.py's "Two regimes"
                    # docstring): what the raw curve does with compensation
                    # off, recorded per outdoor band so a replay can reconstruct
                    # the baseline table's evolution the same way it does the
                    # offset table's, from `learner_bin` plus these. None until
                    # the current band has been observed at all.
                    "learner_baseline_indoor_c": learner.baseline_indoor_c,
                    "learner_baseline_deficit_c": learner.baseline_deficit_c,
                    "learner_baseline_samples": learner.baseline_samples,
                    "learner_baseline_coverage": learner.baseline_coverage,
                    "learner_seeded": learner.seeded,
                }
            )
            # Low-cardinality: only present on the cycle a bin was actually
            # seeded (the off->on transition), not on every row.
            if learner.seeded_bins:
                record["learner_seeded_bins"] = learner.seeded_bins
            # Same reasoning as `learner_freeze_reason` below: the reason string
            # is only informative while compensation is off (see
            # `_observe_baseline`), so it would otherwise dominate the file with
            # a value already implied by `is_active`.
            if learner.baseline_reason:
                record["learner_baseline_reason"] = learner.baseline_reason
            # Low-cardinality diagnostic: logged only when learning is actually
            # blocked, since the full reason every cycle would dominate the file
            # for something reconstructible from the numbers.
            if learner.freeze_reason:
                record["learner_freeze_reason"] = learner.freeze_reason
        if lag is not None:
            record.update(
                {
                    "lag_rise_minutes": lag.rise_minutes,
                    "lag_fall_minutes": lag.fall_minutes,
                    "lag_rise_measured": lag.rise_measured,
                    "lag_fall_measured": lag.fall_measured,
                }
            )
        if self._last_price_forecast is not None:
            record["price_forecast"] = [
                [round(h, 3), p] for h, p in self._last_price_forecast
            ]
        energy_kwh, cost = self._cycle_energy_and_cost(
            self.last_power_data_available, result.current_price, result.price_data_available
        )
        if energy_kwh is not None:
            record["cycle_energy_kwh"] = energy_kwh
        if cost is not None:
            record["cycle_cost"] = cost
        return record

    async def _log_data_point(self, result: HeuristicResult) -> None:
        try:
            await async_log_record(
                self.hass, self.entry.entry_id, self._build_log_record(result)
            )
        except Exception as err:  # noqa: BLE001 - logging never breaks output
            _LOGGER.warning("Could not log data point (ignored): %s", err)
