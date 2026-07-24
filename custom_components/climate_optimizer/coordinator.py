"""DataUpdateCoordinator for ClimateOptimizer.

Fetches indoor/outdoor temperature, a weather forecast (wind + sun
enrichment only), sun geometry, and optionally a Nordpool price, normalizes
units, and hands everything to the pure `heuristic.compute()` function.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfSpeed, UnitOfTemperature
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import SpeedConverter, TemperatureConverter

from .const import (
    CONF_COMFORT_MAX_C,
    CONF_COMFORT_MIN_C,
    CONF_ENABLE_DATA_LOGGING,
    CONF_ENABLE_PRICE_COMPENSATION,
    CONF_ENABLE_WIND_RC,
    CONF_HEATING_CUTOFF_C,
    CONF_INDOOR_TARGET_TEMPERATURE,
    CONF_INDOOR_TEMP_SENSOR,
    CONF_K_INDOOR,
    CONF_K_SUN,
    CONF_K_WIND,
    CONF_MPC_HORIZON_HOURS,
    CONF_MPC_MAX_HEATING_DELTA_C,
    CONF_MPC_MIN_CONFIDENCE,
    CONF_NORDPOOL_PRICE_ENTITY,
    CONF_OUTDOOR_TEMP_SENSOR,
    CONF_PRICE_MAX_DROP_C,
    CONF_PRICE_THRESHOLD_MAX,
    CONF_PRICE_THRESHOLD_START,
    CONF_RC_WIND_REFERENCE_MS,
    CONF_UPDATE_INTERVAL_MINUTES,
    CONF_WEATHER_ENTITY,
    DEFAULT_COMFORT_MAX_C,
    DEFAULT_COMFORT_MIN_C,
    DEFAULT_ENABLE_DATA_LOGGING,
    DEFAULT_ENABLE_PRICE_COMPENSATION,
    DEFAULT_ENABLE_WIND_RC,
    DEFAULT_HEATING_CUTOFF_C,
    DEFAULT_INDOOR_TARGET_TEMPERATURE,
    DEFAULT_K_INDOOR,
    DEFAULT_K_SUN,
    DEFAULT_K_WIND,
    DEFAULT_MPC_HORIZON_HOURS,
    DEFAULT_MPC_MAX_HEATING_DELTA_C,
    DEFAULT_MPC_MIN_CONFIDENCE,
    DEFAULT_PRICE_MAX_DROP_C,
    DEFAULT_PRICE_THRESHOLD_MAX,
    DEFAULT_PRICE_THRESHOLD_START,
    DEFAULT_RC_WIND_REFERENCE_MS,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
)
from .data_logger import async_log_record, log_file_path
from .heuristic import HeuristicInputs, HeuristicParams, HeuristicResult, compute
from .mpc import (
    MPCConfig,
    MPCForecasts,
    MPCModelParams,
    MPCResult,
    plan as mpc_plan,
)
from .rc_model import (
    THETA_ENV_MAX,
    THETA_ENV_MIN,
    THETA_GAIN_MAX,
    THETA_GAIN_MIN,
    THETA_SOLAR_MAX,
    THETA_SOLAR_MIN,
    THETA_WIND_MAX,
    THETA_WIND_MIN,
    RCModelConfig,
    RCModelInputs,
    RCModelResult,
    initial_state as rc_initial_state,
    step as rc_step,
)
from .rc_store import (
    STORAGE_VERSION as RC_STORAGE_VERSION,
    deserialize_state as rc_deserialize_state,
    serialize_state as rc_serialize_state,
    store_key as rc_store_key,
)

_LOGGER = logging.getLogger(__name__)

# Debounce window for persisting the RC shadow-model state. `Store.async_delay_save`
# coalesces every schedule request inside this window into a single disk write,
# and always flushes on Home Assistant shutdown. A short delay is enough: normal
# cycles are minutes apart (so this is ~one write per cycle at most), and the
# real purpose of the debounce is to collapse the *burst* of extra refreshes
# that a watched source recovering (unavailable -> available) can trigger within
# a few seconds into one write instead of several.
RC_STATE_SAVE_DELAY_SECONDS = 30.0


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
        # `rc_result`. Estimator dimensionality (3 or 4 params) is fixed at
        # construction time by whether the optional wind term is enabled —
        # see rc_model.py's module docstring for why this can't just be
        # toggled live without recreating the state. Options changes already
        # trigger a full coordinator reload, so this stays in sync.
        self._rc_state = rc_initial_state(enable_wind=self._rc_config().enable_wind)
        self.rc_result: RCModelResult | None = None
        self._rc_last_monotonic: float | None = None
        # Persistence for the RC estimator state across HA restarts/reloads.
        # Without this, every restart wipes `_rc_state` back to the cold-start
        # prior — losing accumulated learning (theta_gain, the warmup/confidence
        # counters, tau) and, per project history, letting tau drift up into its
        # 500h clip ceiling after frequent restarts. The Store is constructed
        # here (cheap, no I/O); state is loaded once via `async_load_rc_state()`
        # before the first refresh and written debounced after each cycle. Like
        # everything else RC/MPC-related this is STRICTLY ADDITIVE: a load/save
        # failure is caught and logged, never affecting the published output.
        self._rc_store: Store[dict] = Store(
            hass, RC_STORAGE_VERSION, rc_store_key(entry.entry_id)
        )

        # --- Phase 3 shadow/advisory-mode MPC planner (purely additive) ------
        # The MPC planner re-solves a receding-horizon plan every cycle from
        # whatever the RC model currently believes, and stores its latest
        # result here for the MPC diagnostic sensors to read. Like the RC
        # model it NEVER influences `data` (the HeuristicResult driving
        # compensated_outdoor_temp_c); it is observation-only until a
        # deliberate future decision wires it live. Wrapped in the same
        # try/except shadow-safety pattern as the RC model.
        self.mpc_result: MPCResult | None = None
        # The forecast arrays actually used for the latest plan — kept
        # separate from `mpc_result` (not exposed on any live sensor
        # attribute, which stays lean) and read only by the opt-in data
        # logger, so a full multi-hour forecast can be replayed offline
        # later without bloating HA's recorder/entity state size.
        self.mpc_forecasts: MPCForecasts | None = None

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

    @property
    def data_logging_enabled(self) -> bool:
        return bool(
            _entry_value(self.entry, CONF_ENABLE_DATA_LOGGING, DEFAULT_ENABLE_DATA_LOGGING)
        )

    @property
    def data_log_path(self) -> str | None:
        """Resolved path of this entry's local history log, or None if
        logging is off — surfaced on the status sensor so it's discoverable
        without digging through the integration's source."""
        if not self.data_logging_enabled:
            return None
        return str(log_file_path(self.hass, self.entry.entry_id))

    def _rc_config(self) -> RCModelConfig:
        """RC shadow-model estimator tuning, currently just the optional
        wind term. `enable_wind` here must match whatever `_rc_state` was
        constructed with (see __init__) — both read the same config-entry
        option, and an options change reloads the whole entry, so they can't
        drift apart within one coordinator's lifetime.
        """
        entry = self.entry
        return RCModelConfig(
            enable_wind=_entry_value(entry, CONF_ENABLE_WIND_RC, DEFAULT_ENABLE_WIND_RC),
            wind_reference_ms=_entry_value(
                entry, CONF_RC_WIND_REFERENCE_MS, DEFAULT_RC_WIND_REFERENCE_MS
            ),
        )

    def _mpc_config(self) -> MPCConfig:
        """MPC solver tuning. Comfort bounds and target mirror the heuristic's
        (a plan is only meaningful against the same comfort envelope the real
        controller respects); the horizon, heating authority and trust
        threshold are the only user-facing MPC options. Discretisation
        granularity is left at mpc.py's internal defaults."""
        entry = self.entry
        params = self._params()
        return MPCConfig(
            horizon_hours=_entry_value(
                entry, CONF_MPC_HORIZON_HOURS, DEFAULT_MPC_HORIZON_HOURS
            ),
            max_heating_delta_c=_entry_value(
                entry, CONF_MPC_MAX_HEATING_DELTA_C, DEFAULT_MPC_MAX_HEATING_DELTA_C
            ),
            min_confidence=_entry_value(
                entry, CONF_MPC_MIN_CONFIDENCE, DEFAULT_MPC_MIN_CONFIDENCE
            ),
            comfort_min_c=params.comfort_min_c,
            comfort_max_c=params.comfort_max_c,
            indoor_target_c=params.indoor_target_c,
        )

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

        # Advisory mode: compute an MPC plan from the RC model's current
        # beliefs, again without ever affecting `result`.
        await self._update_mpc_shadow(result)

        # Opt-in: append this cycle to the local history log, again without
        # ever affecting `result` — see data_logger.py.
        if self.data_logging_enabled:
            await self._log_data_point(result)

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

        Because `applied_delta_c` is already exactly zero whenever there is no
        real excitation (switch off, or the summer heating-cutoff has made
        compensated == raw even with the switch on), it is the correct signal
        to drive the RC model's lazy gain-dimension expansion: rc_model only
        adds the heat-pump gain dimension the first cycle a genuinely nonzero
        applied delta reaches an accepted update, so an idle warm season never
        winds up an unexcited gain dimension. No extra routing is needed here —
        feeding the true applied delta (as we already do) is what triggers it.
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
                wind_speed_ms=result.wind_speed_ms,
                dt_seconds=dt_seconds,
            )
            self._rc_state, self.rc_result = rc_step(
                self._rc_state, rc_inputs, self._rc_config()
            )
            _LOGGER.debug("RC shadow model: %s", self.rc_result.reason)
            # Debounced persist of the updated estimator state. `_serialize_rc_state`
            # is evaluated at write time (after the delay), so it always captures
            # the latest `_rc_state`; multiple schedules inside the window collapse
            # into a single write. Inside this try/except, so a persistence bug
            # can never break the real output — same shadow-safety contract.
            self._rc_store.async_delay_save(
                self._serialize_rc_state, RC_STATE_SAVE_DELAY_SECONDS
            )
        except Exception as err:  # noqa: BLE001 - shadow mode must never break output
            _LOGGER.warning("RC shadow model update failed (ignored): %s", err)

    def _serialize_rc_state(self) -> dict:
        """Data callback for `Store.async_delay_save`: serialize the current
        RC estimator state. Called at write time, not schedule time."""
        return rc_serialize_state(self._rc_state)

    async def async_load_rc_state(self) -> None:
        """Load persisted RC estimator state, if any, into `_rc_state`.

        Called once from `async_setup_entry` before the first refresh. Strictly
        additive and defensive: on an empty store, a corrupt/incompatible
        payload, a dimensionality mismatch against the currently configured
        `enable_wind`, or any load error, `_rc_state` is left at the fresh
        cold-start prior set in `__init__`. Never raises."""
        try:
            data = await self._rc_store.async_load()
        except Exception as err:  # noqa: BLE001 - never break setup over shadow state
            _LOGGER.warning(
                "Could not load persisted RC shadow state (starting fresh): %s", err
            )
            return
        if data is None:
            _LOGGER.debug("No persisted RC shadow state; starting from cold-start prior")
            return
        restored = rc_deserialize_state(
            data, enable_wind=self._rc_config().enable_wind
        )
        if restored is None:
            _LOGGER.warning(
                "Persisted RC shadow state was incompatible or corrupt "
                "(schema/model version, dimensionality, or structure); "
                "starting from the cold-start prior"
            )
            return
        self._rc_state = restored
        _LOGGER.debug(
            "Restored RC shadow state: %d accepted / %d rejected samples",
            restored.accepted_samples,
            restored.rejected_samples,
        )

    async def async_save_rc_state_now(self) -> None:
        """Flush the RC estimator state to disk immediately.

        Called on config-entry unload/reload, where a pending debounced save
        would otherwise be lost (HA only auto-flushes `async_delay_save` on full
        shutdown, not on an entry reload). Persisting here means a season of
        learning survives an options change too, as long as `enable_wind` is
        unchanged — if it changed, the reloaded coordinator's dimensionality
        check discards the mismatched state and cold-starts, which is correct.
        Strictly additive: any failure is caught and logged."""
        try:
            await self._rc_store.async_save(rc_serialize_state(self._rc_state))
        except Exception as err:  # noqa: BLE001 - never break unload over shadow state
            _LOGGER.warning("Could not persist RC shadow state on unload (ignored): %s", err)

    # --- Phase 3 MPC (shadow/advisory) ---------------------------------------

    @staticmethod
    def _rc_params_pinned(rc_result: RCModelResult, enable_wind: bool) -> bool:
        """Whether any RC parameter currently sits at (within a tiny tolerance
        of) one of its physical clip bounds — a sign the estimator hit a
        guardrail rather than converging, which the MPC trust gate treats as
        "not plausible yet". Checked here (not in the pure mpc module) so mpc.py
        need not know rc_model's bound constants.

        `enable_wind` is passed explicitly rather than inferred from
        `theta_wind != 0.0`: THETA_WIND_MIN is also 0.0, so a wind term
        genuinely clipped down to its floor by real data would be
        indistinguishable from one still sitting untouched at its cold-start
        prior — inferring from the value alone would silently miss that real
        clip event. For the same reason the gain bound is only checked once the
        gain dimension actually exists (`rc_result.gain_modeled`): before then
        `theta_gain` is a not-yet-modeled 0.0, which is not a real clip event
        and must not be treated as one (the MPC trust gate reports "not yet
        excited" separately).
        """
        tol = 1e-6
        checks = [
            (rc_result.theta_env, THETA_ENV_MIN, THETA_ENV_MAX),
            (rc_result.theta_solar, THETA_SOLAR_MIN, THETA_SOLAR_MAX),
        ]
        if rc_result.gain_modeled:
            checks.append((rc_result.theta_gain, THETA_GAIN_MIN, THETA_GAIN_MAX))
        for value, lo, hi in checks:
            if abs(value - lo) <= tol or abs(value - hi) <= tol:
                return True
        if enable_wind:
            if (
                abs(rc_result.theta_wind - THETA_WIND_MIN) <= tol
                or abs(rc_result.theta_wind - THETA_WIND_MAX) <= tol
            ):
                return True
        return False

    def _mpc_model_params(self, rc_result: RCModelResult) -> MPCModelParams:
        rc_config = self._rc_config()
        return MPCModelParams(
            theta_env=rc_result.theta_env,
            theta_gain=rc_result.theta_gain,
            theta_solar=rc_result.theta_solar,
            theta_wind=rc_result.theta_wind,
            enable_wind=rc_config.enable_wind,
            wind_reference_ms=rc_config.wind_reference_ms,
            confidence=rc_result.confidence,
            accepted_samples=rc_result.accepted_samples,
            params_pinned=self._rc_params_pinned(rc_result, rc_config.enable_wind),
            gain_modeled=rc_result.gain_modeled,
        )

    @staticmethod
    def _align_series(
        entries: list[tuple[datetime, float]],
        now: datetime,
        steps: int,
        step_hours: float,
    ) -> tuple[list[float], int]:
        """Sample a sorted (start_time, value) forecast series onto the MPC's
        step grid. Returns (values, valid_count). Beyond the last forecast
        entry the last value is held (persistence); `valid_count` is how many
        leading steps fell within the forecast's real coverage."""
        if not entries:
            return [0.0] * steps, 0
        entries = sorted(entries, key=lambda e: e[0])
        first_dt = entries[0][0]
        last_dt = entries[-1][0]
        if len(entries) > 1:
            est_step = (last_dt - first_dt) / (len(entries) - 1)
        else:
            est_step = timedelta(hours=1)
        covered_until = last_dt + est_step
        values: list[float] = []
        valid = 0
        for k in range(steps):
            t = now + timedelta(hours=step_hours * k)
            value = entries[0][1]
            for dt, v in entries:
                if dt <= t:
                    value = v
                else:
                    break
            values.append(value)
            if t < covered_until:
                valid += 1
        return values, valid

    def _read_price_forecast(
        self, steps: int, step_hours: float, fallback_price: float | None
    ) -> tuple[list[float], int]:
        """Multi-hour price array from the Nordpool sensor's `raw_today` /
        `raw_tomorrow` attributes (the well-known HACS Nordpool shape:
        lists of {start, end, value}). Falls back to a flat current price when
        those attributes are absent, so the plan still runs (as pure
        energy-minimisation, with nothing to load-shift)."""
        entity_id = _entry_value(self.entry, CONF_NORDPOOL_PRICE_ENTITY, None)
        flat = float(fallback_price) if fallback_price is not None else 1.0
        if not entity_id:
            # No price configured at all — a flat price is the best we can do,
            # and there is nothing further to fetch, so it is not a truncation.
            return [flat] * steps, steps
        state = self.hass.states.get(entity_id)
        if not _state_is_usable(state):
            return [flat] * steps, 0
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
        if not entries:
            return [flat] * steps, 0
        return self._align_series(entries, dt_util.now(), steps, step_hours)

    async def _read_weather_forecast_arrays(
        self, steps: int, step_hours: float, fallback_outdoor_c: float
    ) -> tuple[list[float], list[float], int]:
        """Multi-hour outdoor-temperature and wind arrays from the weather
        integration's hourly `weather.get_forecasts` (the FULL array, not just
        forecast[0]). Falls back to holding the current outdoor temperature
        (persistence) with zero wind when no forecast is available."""
        weather_entity_id = _entry_value(self.entry, CONF_WEATHER_ENTITY, None)
        weather_state = self.hass.states.get(weather_entity_id)
        outdoor_fallback = [fallback_outdoor_c] * steps
        wind_fallback = [0.0] * steps
        if not _state_is_usable(weather_state):
            return outdoor_fallback, wind_fallback, 0
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": weather_entity_id, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
            forecast = response[weather_entity_id]["forecast"]
        except Exception as err:  # noqa: BLE001 - soft-degrade on any forecast failure
            _LOGGER.debug("MPC hourly forecast unavailable for %s: %s", weather_entity_id, err)
            return outdoor_fallback, wind_fallback, 0
        if not forecast:
            return outdoor_fallback, wind_fallback, 0

        temp_unit = weather_state.attributes.get("temperature_unit", UnitOfTemperature.CELSIUS)
        wind_unit = weather_state.attributes.get(
            "wind_speed_unit", UnitOfSpeed.METERS_PER_SECOND
        )
        temp_entries: list[tuple[datetime, float]] = []
        wind_entries: list[tuple[datetime, float]] = []
        for item in forecast:
            when = item.get("datetime")
            if isinstance(when, str):
                when = dt_util.parse_datetime(when)
            if when is None:
                continue
            when = dt_util.as_local(when)
            temp = item.get("temperature")
            if temp is not None:
                try:
                    temp_entries.append(
                        (when, TemperatureConverter.convert(
                            float(temp), temp_unit, UnitOfTemperature.CELSIUS
                        ))
                    )
                except (TypeError, ValueError):
                    pass
            wind = item.get("wind_speed")
            if wind is not None:
                try:
                    wind_entries.append(
                        (when, SpeedConverter.convert(
                            float(wind), wind_unit, UnitOfSpeed.METERS_PER_SECOND
                        ))
                    )
                except (TypeError, ValueError):
                    pass

        now = dt_util.now()
        if temp_entries:
            outdoor, temp_valid = self._align_series(temp_entries, now, steps, step_hours)
        else:
            outdoor, temp_valid = outdoor_fallback, 0
        if wind_entries:
            wind, _wind_valid = self._align_series(wind_entries, now, steps, step_hours)
        else:
            wind = wind_fallback
        return outdoor, wind, temp_valid

    async def _update_mpc_shadow(self, result: HeuristicResult) -> None:
        """Compute this cycle's advisory MPC plan and store it for the MPC
        diagnostic sensors. Strictly additive and wrapped like the RC shadow
        model: any failure is swallowed (logged at warning) so a bug in the
        experimental planner can never break the real heuristic output. Never
        touches `data`/`compensated_outdoor_temp_c`."""
        try:
            if self.rc_result is None:
                # No RC estimate yet (very first cycles); nothing to plan on.
                return
            config = self._mpc_config()
            step_hours = config.step_hours
            steps = max(1, int(round(config.horizon_hours / step_hours)))

            price, price_valid = self._read_price_forecast(
                steps, step_hours, result.current_price
            )
            outdoor, wind, weather_valid = await self._read_weather_forecast_arrays(
                steps, step_hours, result.raw_outdoor_temp_c
            )
            # Solar is not forecast over the horizon yet (would need per-hour
            # future sun elevation): assume no solar gain, which is the safe
            # direction (never over-counts free heat, so plans stay
            # comfort-safe). Documented as a known limitation / next step.
            solar = [0.0] * steps

            forecasts = MPCForecasts(
                price=tuple(price[:steps]),
                outdoor_temp_c=tuple(outdoor[:steps]),
                solar_effect=tuple(solar[:steps]),
                wind_speed_ms=tuple(wind[:steps]),
                valid_steps=min(price_valid, weather_valid),
            )
            self.mpc_forecasts = forecasts
            self.mpc_result = mpc_plan(
                result.indoor_temp_c,
                self._mpc_model_params(self.rc_result),
                forecasts,
                config,
            )
            _LOGGER.debug("MPC advisory plan: %s", self.mpc_result.reason)
        except Exception as err:  # noqa: BLE001 - advisory mode must never break output
            _LOGGER.warning("MPC advisory update failed (ignored): %s", err)

    # --- Opt-in local history logging (data_logger.py) -----------------------

    def _build_log_record(self, result: HeuristicResult) -> dict:
        """Flatten this cycle's raw inputs and computed results into one
        record for the local history log. Raw physical data first (what a
        future offline re-fit of rc_model.py would actually need), computed
        results appended for cross-reference against what the live system
        did at the time. See data_logger.py for why/where this is written.
        """
        applied_delta_c = (
            (result.compensated_outdoor_temp_c - result.raw_outdoor_temp_c)
            if self.is_active
            else 0.0
        )
        record: dict = {
            "ts": dt_util.utcnow().isoformat(),
            "is_active": self.is_active,
            "indoor_target_c": self.indoor_target_c,
            "indoor_temp_c": result.indoor_temp_c,
            "indoor_data_available": result.indoor_data_available,
            "raw_outdoor_temp_c": result.raw_outdoor_temp_c,
            "wind_speed_ms": result.wind_speed_ms,
            "wind_data_available": result.wind_data_available,
            "cloud_coverage_pct": result.cloud_coverage_pct,
            "cloud_data_available": result.cloud_data_available,
            "solar_effect": result.solar_effect,
            "current_price": result.current_price,
            "price_data_available": result.price_data_available,
            "compensated_outdoor_temp_c": result.compensated_outdoor_temp_c,
            "applied_delta_c": applied_delta_c,
            "heating_cutoff_engaged": result.heating_cutoff_engaged,
        }
        if self.rc_result is not None:
            record.update(
                {
                    "rc_theta_env": self.rc_result.theta_env,
                    "rc_theta_gain": self.rc_result.theta_gain,
                    "rc_gain_modeled": self.rc_result.gain_modeled,
                    "rc_theta_solar": self.rc_result.theta_solar,
                    "rc_theta_wind": self.rc_result.theta_wind,
                    "rc_confidence": self.rc_result.confidence,
                    "rc_accepted_samples": self.rc_result.accepted_samples,
                }
            )
        if self.mpc_result is not None:
            record.update(
                {
                    "mpc_status": self.mpc_result.status,
                    "mpc_trustworthy": self.mpc_result.trustworthy,
                    "mpc_recommended_delta_c": self.mpc_result.recommended_delta_c,
                }
            )
        if self.mpc_forecasts is not None:
            # The exact multi-hour forecast MPC planned against this cycle —
            # needed to faithfully replay/backtest a past decision later,
            # since forecasts get revised over time and the realised values
            # aren't a substitute for what was actually known at the time.
            # Logged at whatever mpc_horizon_hours/step is currently
            # configured, not a separate fixed window (see README).
            mpc_config = self._mpc_config()
            record.update(
                {
                    "mpc_horizon_hours": mpc_config.horizon_hours,
                    "mpc_step_hours": mpc_config.step_hours,
                    "mpc_forecast_valid_steps": self.mpc_forecasts.valid_steps,
                    "mpc_forecast_price": list(self.mpc_forecasts.price),
                    "mpc_forecast_outdoor_temp_c": list(self.mpc_forecasts.outdoor_temp_c),
                    "mpc_forecast_wind_speed_ms": list(self.mpc_forecasts.wind_speed_ms),
                    "mpc_forecast_solar_effect": list(self.mpc_forecasts.solar_effect),
                }
            )
        return record

    async def _log_data_point(self, result: HeuristicResult) -> None:
        """Append this cycle to the local history log. Strictly additive,
        same shadow-safety pattern as the RC/MPC updates: any failure here
        is swallowed (logged at warning) so a disk/permissions problem can
        never affect the real output."""
        try:
            record = self._build_log_record(result)
            await async_log_record(self.hass, self.entry.entry_id, record)
        except Exception as err:  # noqa: BLE001 - logging must never break output
            _LOGGER.warning("ClimateOptimizer data logging failed (ignored): %s", err)
