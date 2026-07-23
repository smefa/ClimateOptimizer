"""DataUpdateCoordinator for ClimateOptimizer.

Fetches indoor/outdoor temperature, a weather forecast (wind + sun
enrichment only), sun geometry, and optionally a Nordpool price, normalizes
units, and hands everything to the pure `heuristic.compute()` function.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfSpeed, UnitOfTemperature
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.unit_conversion import SpeedConverter, TemperatureConverter

from .const import (
    CONF_COMFORT_MAX_C,
    CONF_COMFORT_MIN_C,
    CONF_ENABLE_PRICE_COMPENSATION,
    CONF_HEATING_CUTOFF_C,
    CONF_INDOOR_TARGET_TEMPERATURE,
    CONF_INDOOR_TEMP_SENSOR,
    CONF_K_INDOOR,
    CONF_K_SUN,
    CONF_K_WIND,
    CONF_NORDPOOL_PRICE_ENTITY,
    CONF_OUTDOOR_TEMP_SENSOR,
    CONF_PRICE_MAX_DROP_C,
    CONF_PRICE_THRESHOLD_MAX,
    CONF_PRICE_THRESHOLD_START,
    CONF_UPDATE_INTERVAL_MINUTES,
    CONF_WEATHER_ENTITY,
    DEFAULT_COMFORT_MAX_C,
    DEFAULT_COMFORT_MIN_C,
    DEFAULT_ENABLE_PRICE_COMPENSATION,
    DEFAULT_HEATING_CUTOFF_C,
    DEFAULT_INDOOR_TARGET_TEMPERATURE,
    DEFAULT_K_INDOOR,
    DEFAULT_K_SUN,
    DEFAULT_K_WIND,
    DEFAULT_PRICE_MAX_DROP_C,
    DEFAULT_PRICE_THRESHOLD_MAX,
    DEFAULT_PRICE_THRESHOLD_START,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
)
from .heuristic import HeuristicInputs, HeuristicParams, HeuristicResult, compute
from .rc_model import (
    RCModelInputs,
    RCModelResult,
    initial_state as rc_initial_state,
    step as rc_step,
)

_LOGGER = logging.getLogger(__name__)


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


class ClimateOptimizerCoordinator(DataUpdateCoordinator[HeuristicResult]):
    """Fetches inputs each cycle and computes the heuristic result."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        interval_minutes = _entry_value(
            entry, CONF_UPDATE_INTERVAL_MINUTES, DEFAULT_UPDATE_INTERVAL_MINUTES
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=interval_minutes),
        )
        # --- Phase 2 shadow-mode RC estimator (purely additive) --------------
        # Persistent estimator state and the latest result live as instance
        # attributes; the RC model NEVER influences `data` (the HeuristicResult
        # that drives compensated_outdoor_temp_c). Diagnostic sensors read
        # `rc_result`.
        self._rc_state = rc_initial_state()
        self.rc_result: RCModelResult | None = None
        self._rc_last_monotonic: float | None = None

        # --- Activation switch (learn mode vs live) ---------------------------
        # Default OFF ("learn mode"): the compensated-temperature sensor
        # publishes the raw outdoor temperature until the user explicitly
        # switches this on, per switch.py (which restores the last state
        # across restarts; this is only the pre-restore default). The
        # heuristic itself always runs and is always exposed as the
        # `recommended_compensated_outdoor_temp_c` attribute regardless of
        # this flag — only the published *state* and what the RC model treats
        # as "actually applied" are gated by it.
        self.is_active: bool = False

        # --- Indoor target temperature (live, number.py) ----------------------
        # Backed by an in-memory coordinator value rather than a config entry
        # option: changing an option triggers a full entry reload (see
        # _async_update_listener in __init__.py), which would recreate the
        # coordinator and wipe the RC estimator's learning progress on every
        # nudge of the target temperature — exactly the kind of value people
        # adjust often (day/night schedules, automations). Seeded from the
        # config entry here for the very first run; number.py's RestoreEntity
        # takes over after that, same pattern as `is_active` above.
        self.indoor_target_c: float = _entry_value(
            entry, CONF_INDOOR_TARGET_TEMPERATURE, DEFAULT_INDOOR_TARGET_TEMPERATURE
        )

    def watched_entity_ids(self) -> list[str]:
        """Source entities whose state changes should trigger an immediate
        refresh, instead of waiting for the next polled interval.

        HA's DataUpdateCoordinator skips notifying entities on consecutive
        identical failures (see homeassistant/helpers/update_coordinator.py),
        so there's no external visibility into whether its background timer
        is still quietly retrying. Rather than depend on that timing, we
        react directly to the required/soft-degraded sources' own state
        changes (in particular unavailable -> available) so recovery is fast
        and doesn't depend on guessing the coordinator's internal schedule.
        """
        ids = [
            _entry_value(self.entry, CONF_OUTDOOR_TEMP_SENSOR, None),
            _entry_value(self.entry, CONF_INDOOR_TEMP_SENSOR, None),
        ]
        return [entity_id for entity_id in ids if entity_id]

    @property
    def price_configured(self) -> bool:
        """Whether a Nordpool price entity was ever set, regardless of
        whether the price *feature* is currently enabled. Used by the status
        sensor to avoid flagging price as "degraded" when it was simply never
        configured in the first place.
        """
        return bool(_entry_value(self.entry, CONF_NORDPOOL_PRICE_ENTITY, None))

    def _params(self) -> HeuristicParams:
        entry = self.entry
        return HeuristicParams(
            indoor_target_c=self.indoor_target_c,
            enable_price_compensation=_entry_value(
                entry, CONF_ENABLE_PRICE_COMPENSATION, DEFAULT_ENABLE_PRICE_COMPENSATION
            ),
            k_indoor=_entry_value(entry, CONF_K_INDOOR, DEFAULT_K_INDOOR),
            k_wind=_entry_value(entry, CONF_K_WIND, DEFAULT_K_WIND),
            k_sun=_entry_value(entry, CONF_K_SUN, DEFAULT_K_SUN),
            comfort_min_c=_entry_value(entry, CONF_COMFORT_MIN_C, DEFAULT_COMFORT_MIN_C),
            comfort_max_c=_entry_value(entry, CONF_COMFORT_MAX_C, DEFAULT_COMFORT_MAX_C),
            price_threshold_start=_entry_value(
                entry, CONF_PRICE_THRESHOLD_START, DEFAULT_PRICE_THRESHOLD_START
            ),
            price_threshold_max=_entry_value(
                entry, CONF_PRICE_THRESHOLD_MAX, DEFAULT_PRICE_THRESHOLD_MAX
            ),
            price_max_drop_c=_entry_value(
                entry, CONF_PRICE_MAX_DROP_C, DEFAULT_PRICE_MAX_DROP_C
            ),
            heating_cutoff_c=_entry_value(
                entry, CONF_HEATING_CUTOFF_C, DEFAULT_HEATING_CUTOFF_C
            ),
        )

    def _read_indoor_temp_c(self) -> tuple[float | None, bool]:
        """Return (indoor_temp_c, indoor_data_available).

        Unlike the weather entity, a missing indoor sensor doesn't leave us
        with nothing to report: we can still fall back to publishing the raw
        outdoor temperature (see heuristic.compute), so this soft-degrades
        rather than raising UpdateFailed.
        """
        entity_id = _entry_value(self.entry, CONF_INDOOR_TEMP_SENSOR, None)
        state = self.hass.states.get(entity_id)
        if not _state_is_usable(state):
            _LOGGER.warning(
                "Indoor temperature sensor %s is unavailable; publishing the "
                "raw outdoor temperature uncompensated for indoor error this cycle",
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
        """The dedicated outdoor sensor is the sole source of the current
        outdoor temperature. There's no sane fallback if it's missing (unlike
        the indoor sensor, there's nothing left to publish), so this is the
        one required-source read that still hard-fails via UpdateFailed.
        """
        entity_id = _entry_value(self.entry, CONF_OUTDOOR_TEMP_SENSOR, None)
        state = self.hass.states.get(entity_id)
        if not _state_is_usable(state):
            raise UpdateFailed(f"Outdoor temperature sensor {entity_id} is unavailable")
        value = _as_float(state)
        if value is None:
            raise UpdateFailed(f"Outdoor temperature sensor {entity_id} has no numeric state")
        unit = state.attributes.get("unit_of_measurement", UnitOfTemperature.CELSIUS)
        return TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)

    async def _read_forecast(
        self,
    ) -> tuple[float, bool, float | None, bool]:
        """Return (wind_speed_ms, wind_data_available, cloud_coverage_pct, cloud_data_available).

        Wind and cloud/sun are tracked independently: not every weather
        integration provides both, and a missing wind_speed value shouldn't
        cause a perfectly good cloud_coverage reading to be discarded (or
        vice versa) — previously it did, and the combined flag also silently
        treated "no cloud data" as if it were fine. Tries the hourly forecast
        first, then daily, filling in whichever field(s) are still missing
        from whichever type provides them, and stops once both are found.
        The weather entity is enrichment only now (raw outdoor temperature
        comes from a dedicated sensor), so any failure here — including the
        entity itself being unavailable — soft-degrades rather than failing
        the whole update.
        """
        weather_entity_id = _entry_value(self.entry, CONF_WEATHER_ENTITY, None)
        weather_state = self.hass.states.get(weather_entity_id)
        if not _state_is_usable(weather_state):
            _LOGGER.warning(
                "Weather entity %s is unavailable; continuing without wind/sun forecast data",
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
            except Exception as err:  # noqa: BLE001 - soft-degrade on any forecast failure
                _LOGGER.debug(
                    "Forecast type %s unavailable for %s: %s",
                    forecast_type,
                    weather_entity_id,
                    err,
                )

        wind_data_available = wind_speed_ms is not None
        cloud_data_available = cloud_coverage_pct is not None
        if not wind_data_available:
            _LOGGER.warning(
                "Could not retrieve a wind forecast for %s; wind adjustment "
                "will contribute 0 this cycle",
                weather_entity_id,
            )
        if not cloud_data_available:
            _LOGGER.warning(
                "Could not retrieve a cloud/sun forecast for %s; solar term "
                "will assume clear sky this cycle",
                weather_entity_id,
            )
        return (
            wind_speed_ms if wind_speed_ms is not None else 0.0,
            wind_data_available,
            cloud_coverage_pct,
            cloud_data_available,
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
                "Nordpool price entity %s is unavailable; price compensation "
                "will contribute 0 this cycle",
                entity_id,
            )
            return None, False
        value = _as_float(state)
        if value is None:
            _LOGGER.warning("Nordpool price entity %s has no numeric state", entity_id)
            return None, False
        return value, True

    async def _async_update_data(self) -> HeuristicResult:
        raw_outdoor_temp_c = self._read_raw_outdoor_temp_c()
        indoor_temp_c, indoor_ok = self._read_indoor_temp_c()
        (
            wind_speed_ms,
            wind_ok,
            cloud_coverage_pct,
            cloud_ok,
        ) = await self._read_forecast()
        sun_elevation_deg = self._read_sun_elevation()
        current_price, price_ok = self._read_price()

        inputs = HeuristicInputs(
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
        )
        result = compute(inputs, self._params())

        # Shadow mode: feed the RC estimator but never let it affect `result`.
        self._update_rc_shadow_model(result)

        return result

    def _update_rc_shadow_model(self, result: HeuristicResult) -> None:
        """Advance the shadow RC estimator with this cycle's data.

        Strictly additive: any failure here is swallowed (logged at warning)
        so a bug in the experimental estimator can never break the real
        output. The proxy control signal is the compensation delta that was
        *actually applied* this cycle — zero while `is_active` is False
        (learn mode publishes the raw outdoor temperature, so nothing was
        really applied), not the heuristic's hypothetical recommendation.
        Feeding the model an intervention that never happened would corrupt
        the heat-pump-gain estimate; the model can still learn the envelope
        time constant and solar gain from passive data while inactive, it
        just can't learn anything about heat-pump gain without real
        excitation on that channel. The actual outdoor temperature is always
        the envelope driver, active or not.
        """
        try:
            now = time.monotonic()
            if self._rc_last_monotonic is None:
                # No previous cycle to measure against; the estimator treats
                # this as a cold-start anchor regardless of the dt passed.
                dt_seconds = self.update_interval.total_seconds()
            else:
                dt_seconds = now - self._rc_last_monotonic
            self._rc_last_monotonic = now

            applied_delta_c = (
                (result.compensated_outdoor_temp_c - result.raw_outdoor_temp_c)
                if self.is_active
                else 0.0
            )
            rc_inputs = RCModelInputs(
                indoor_temp_c=result.indoor_temp_c,
                indoor_data_available=result.indoor_data_available,
                outdoor_temp_c=result.raw_outdoor_temp_c,
                compensation_delta_c=applied_delta_c,
                solar_effect=result.solar_effect,
                dt_seconds=dt_seconds,
            )
            self._rc_state, self.rc_result = rc_step(self._rc_state, rc_inputs)
            _LOGGER.debug("RC shadow model: %s", self.rc_result.reason)
        except Exception as err:  # noqa: BLE001 - shadow mode must never break output
            _LOGGER.warning("RC shadow model update failed (ignored): %s", err)
