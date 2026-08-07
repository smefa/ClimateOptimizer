"""DataUpdateCoordinator for ClimateOptimizer.

Fetches indoor/outdoor temperature, a weather forecast (wind + sun
enrichment only), sun geometry, and optionally a Nordpool price, normalizes
units, and hands everything to the pure `heuristic.compute()` function.
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
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import PowerConverter, SpeedConverter, TemperatureConverter

from .const import (
    CONF_COMFORT_MAX_C,
    CONF_COLD_TAPER_FULL_C,
    CONF_COLD_TAPER_MIN_FACTOR,
    CONF_COLD_TAPER_START_C,
    CONF_COMFORT_MIN_C,
    CONF_ENABLE_DATA_LOGGING,
    CONF_ENABLE_PRICE_COMPENSATION,
    CONF_ENABLE_SOLAR_INPUT,
    CONF_ENABLE_WIND_INPUT,
    CONF_ENABLE_WIND_RC,
    CONF_HEATING_CUTOFF_C,
    CONF_HEATING_TYPE,
    CONF_INDOOR_TARGET_TEMPERATURE,
    CONF_INDOOR_TEMP_SENSOR,
    CONF_K_INDOOR,
    CONF_K_PRICE,
    CONF_K_SUN,
    CONF_K_WIND,
    CONF_MPC_HORIZON_HOURS,
    CONF_MPC_MAX_HEATING_DELTA_C,
    CONF_MPC_MIN_CONFIDENCE,
    CONF_NORDPOOL_PRICE_ENTITY,
    CONF_OHMONWIFI_HOST,
    CONF_OUTDOOR_TEMP_SENSOR,
    CONF_OUTPUT_NUMBER_ENTITY,
    CONF_POWER_SENSOR,
    CONF_PRECHARGE_MAX_BOOST_C,
    CONF_PRICE_COMFORT_TIER,
    CONF_PRICE_MAX_DROP_C,
    CONF_PRICE_THRESHOLD_MAX,
    CONF_PRICE_THRESHOLD_START,
    CONF_RC_WIND_REFERENCE_MS,
    CONF_TUNING_MODE,
    CONF_UPDATE_INTERVAL_MINUTES,
    CONF_WEATHER_ENTITY,
    DEFAULT_COLD_TAPER_FULL_C,
    DEFAULT_COLD_TAPER_MIN_FACTOR,
    DEFAULT_COLD_TAPER_START_C,
    DEFAULT_COMFORT_MAX_C,
    DEFAULT_COMFORT_MIN_C,
    DEFAULT_ENABLE_DATA_LOGGING,
    DEFAULT_ENABLE_PRICE_COMPENSATION,
    DEFAULT_ENABLE_SOLAR_INPUT,
    DEFAULT_ENABLE_WIND_INPUT,
    DEFAULT_ENABLE_WIND_RC,
    DEFAULT_HEATING_CUTOFF_C,
    DEFAULT_HEATING_TYPE,
    DEFAULT_INDOOR_TARGET_TEMPERATURE,
    DEFAULT_K_INDOOR,
    DEFAULT_K_PRICE,
    DEFAULT_K_SUN,
    DEFAULT_K_WIND,
    DEFAULT_MPC_HORIZON_HOURS,
    DEFAULT_MPC_MAX_HEATING_DELTA_C,
    DEFAULT_MPC_MIN_CONFIDENCE,
    DEFAULT_PRECHARGE_MAX_BOOST_C,
    DEFAULT_PRICE_COMFORT_TIER,
    DEFAULT_PRICE_MAX_DROP_C,
    DEFAULT_PRICE_THRESHOLD_MAX,
    DEFAULT_PRICE_THRESHOLD_START,
    DEFAULT_RC_WIND_REFERENCE_MS,
    DEFAULT_TUNING_MODE,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
)
from .autotune import (
    TUNING_MODE_AUTO,
    TUNING_MODES,
    AutotuneInputs,
    AutotuneResult,
    derive as autotune_derive,
    log_fields as autotune_log_fields,
)
from .data_logger import async_log_record, log_file_path
from .heuristic import (
    HeuristicInputs,
    HeuristicParams,
    HeuristicResult,
    cold_taper_factor,
    compute,
    resolve_price_tier,
)
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
    RCModelState,
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

# Skip re-pushing to the optional output number entity / OhmOnWifi device when
# the value hasn't meaningfully moved since the last successful push, so a
# real hardware input (e.g. a Modbus-backed heat-pump register, or OhmOnWifi's
# resistance output) isn't rewritten every cycle for sub-noise-floor changes.
OUTPUT_NUMBER_WRITE_TOLERANCE_C = 0.05

# Timeout for the optional direct OhmOnWifi local-API push. It's a plain HTTP
# GET to a device on the local network (see /AT/?T=<value> in Ohmigo's API
# doc), so a short timeout is appropriate — no point blocking a whole
# coordinator cycle on a device that's gone unreachable.
OHMONWIFI_REQUEST_TIMEOUT_SECONDS = 10.0


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
        _rc_cfg = self._rc_config()
        self._rc_state = rc_initial_state(
            enable_solar=_rc_cfg.enable_solar, enable_wind=_rc_cfg.enable_wind
        )
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
        # The exact price forecast (hours_from_now, price) the heuristic's price
        # decision used this cycle — stashed so the data logger can replay why
        # it braked / pre-charged later (forecasts get revised, so the realised
        # prices are not a substitute for what was known at the time).
        self._last_price_forecast: tuple[tuple[float, float], ...] | None = None

        # --- Optional power draw (diagnostic echo + local-log cost figures) ---
        # Purely informational: never read by the heuristic/RC/MPC. Latest
        # reading is kept for sensor.py's echo sensor regardless of whether
        # data logging is on; the monotonic timer is only advanced when a
        # cycle is actually logged (see _cycle_energy_and_cost), since it's
        # only used there.
        self.last_power_w: float | None = None
        self.last_power_data_available: bool = False
        self._energy_last_monotonic: float | None = None

        # --- Optional output push (HA number entity, and/or OhmOnWifi direct,
        # both independent and both pushed to every cycle if configured) ----
        # Each channel tracks the last value it actually wrote, separately —
        # so an unchanged value doesn't keep rewriting a real device register,
        # and so one channel failing (e.g. OhmOnWifi unreachable) doesn't
        # suppress a retry there just because the other channel succeeded.
        self._last_ohmonwifi_value_c: float | None = None
        self._last_output_number_entity_value_c: float | None = None

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

        # --- Price comfort tier (live, select.py) -----------------------------
        # Same in-memory-plus-RestoreEntity pattern as indoor_target_c above:
        # the tier is a comfort/savings knob people flip often (and via
        # automations), so backing it with a config option — which would reload
        # the entry and wipe RC learning on every change — is the wrong home.
        # Seeded from the config entry for the first run; select.py restores it
        # thereafter.
        self.price_comfort_tier: str = _entry_value(
            entry, CONF_PRICE_COMFORT_TIER, DEFAULT_PRICE_COMFORT_TIER
        )

        # --- Tuning mode (live, select.py) ------------------------------------
        # Manual = the configured k_* coefficients; Auto = coefficients derived
        # from the learned RC model (see autotune.py). Same
        # option-seeds-an-entity pattern as the tier above: the whole point of
        # the switch is easy A/B comparison, and routing every flip through a
        # config option would reload the entry mid-experiment.
        self.tuning_mode: str = _entry_value(
            entry, CONF_TUNING_MODE, DEFAULT_TUNING_MODE
        )
        # Latest derivation, recomputed every cycle in BOTH modes so the
        # derived-vs-manual comparison is always available on the diagnostic
        # sensors regardless of which set is actually driving the output.
        self.autotune_result: AutotuneResult | None = None

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
    def output_number_entity_id(self) -> str | None:
        """The optional `number.*` entity to push the published compensated
        outdoor temperature into, or None if not configured."""
        return _entry_value(self.entry, CONF_OUTPUT_NUMBER_ENTITY, None) or None

    @property
    def ohmonwifi_host(self) -> str | None:
        """Optional hostname/IP of an OhmOnWifi/Ohmigo device to push the
        published compensated outdoor temperature to directly over its own
        local HTTP API, bypassing `output_number_entity_id` entirely. None if
        not configured. Independent of `output_number_entity_id` — if both
        are set, both are pushed to every cycle."""
        return _entry_value(self.entry, CONF_OHMONWIFI_HOST, None) or None

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

    @property
    def rc_estimator_state(self) -> RCModelState:
        """Read-only view of the raw RLS estimator state, for diagnostics.

        Exposed because the covariance matrix appears on no sensor and is the
        artefact that distinguishes "still converging" from "covariance has
        wound up and the fit has stopped responding". Callers must treat it as
        immutable — `RCModelState` is a frozen dataclass, and `step()` returns a
        fresh one rather than mutating.
        """
        return self._rc_state

    @property
    def solar_input_enabled(self) -> bool:
        return bool(
            _entry_value(self.entry, CONF_ENABLE_SOLAR_INPUT, DEFAULT_ENABLE_SOLAR_INPUT)
        )

    @property
    def wind_input_enabled(self) -> bool:
        return bool(
            _entry_value(self.entry, CONF_ENABLE_WIND_INPUT, DEFAULT_ENABLE_WIND_INPUT)
        )

    @property
    def weather_needed(self) -> bool:
        """Whether the optional weather entity is worth reading at all.

        Its only two consumers are the wind and cloud/sun terms, so with both
        inputs switched off there is nothing to fetch and the per-cycle
        forecast service calls (and their "unavailable" warnings) are skipped
        entirely."""
        return self.solar_input_enabled or self.wind_input_enabled

    def _rc_config(self) -> RCModelConfig:
        """RC shadow-model estimator tuning: which optional dimensions the
        estimator carries, and the wind normalisation reference.

        `enable_solar`/`enable_wind` here must match whatever `_rc_state` was
        constructed with (see __init__) — both read the same config-entry
        options, and an options change reloads the whole entry, so they can't
        drift apart within one coordinator's lifetime.

        The RC wind dimension requires BOTH the user-facing wind input and the
        advanced `enable_wind_rc` flag. They are not redundant: the first says
        "wind is available and worth compensating for", the second opts into the
        statistically riskier business of *estimating* a wind coefficient, which
        rc_model.py documents as fragile for typical houses because the wind
        regressor is collinear with the envelope term. Solar has no such hazard
        and so needs no second flag.
        """
        entry = self.entry
        return RCModelConfig(
            enable_solar=self.solar_input_enabled,
            enable_wind=(
                self.wind_input_enabled
                and _entry_value(entry, CONF_ENABLE_WIND_RC, DEFAULT_ENABLE_WIND_RC)
            ),
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

    def manual_k_values(self) -> tuple[float, float, float, float]:
        """The hand-configured (k_indoor, k_wind, k_sun, k_price).

        These stay meaningful in Auto mode: they are the values the derivation
        blends *out of* as evidence accumulates, and the values it falls back to
        whenever the model fails a trust gate.
        """
        entry = self.entry
        return (
            _entry_value(entry, CONF_K_INDOOR, DEFAULT_K_INDOOR),
            _entry_value(entry, CONF_K_WIND, DEFAULT_K_WIND),
            _entry_value(entry, CONF_K_SUN, DEFAULT_K_SUN),
            _entry_value(entry, CONF_K_PRICE, DEFAULT_K_PRICE),
        )

    def _params(self) -> HeuristicParams:
        """Build this cycle's heuristic parameters.

        In Auto mode the four k_* coefficients and the cold taper come from
        `self.autotune_result` (computed earlier this cycle in
        `_async_update_data`) instead of the config entry. Everything else —
        comfort bounds, target, cutoff, price thresholds — is user preference
        and is never derived.

        `autotune.derive` guarantees its `*_effective` fields equal the manual
        values whenever the model is not trustworthy, so this branch does not
        need its own fallback logic: reading the effective values unconditionally
        in Auto mode is already safe.
        """
        entry = self.entry
        k_indoor, k_wind, k_sun, k_price = self.manual_k_values()
        cold_taper_override: float | None = None
        tuned = self.autotune_result
        if self.tuning_mode == TUNING_MODE_AUTO and tuned is not None:
            k_indoor = tuned.k_indoor_effective
            k_wind = tuned.k_wind_effective
            k_sun = tuned.k_sun_effective
            k_price = tuned.k_price_effective
            cold_taper_override = tuned.cold_taper_effective
        return HeuristicParams(
            indoor_target_c=self.indoor_target_c,
            enable_price_compensation=_entry_value(
                entry, CONF_ENABLE_PRICE_COMPENSATION, DEFAULT_ENABLE_PRICE_COMPENSATION
            ),
            k_indoor=k_indoor,
            k_wind=k_wind,
            k_sun=k_sun,
            enable_solar_input=self.solar_input_enabled,
            enable_wind_input=self.wind_input_enabled,
            cold_taper_override=cold_taper_override,
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
            price_comfort_tier=self.price_comfort_tier,
            k_price=k_price,
            cold_taper_start_c=_entry_value(
                entry, CONF_COLD_TAPER_START_C, DEFAULT_COLD_TAPER_START_C
            ),
            cold_taper_full_c=_entry_value(
                entry, CONF_COLD_TAPER_FULL_C, DEFAULT_COLD_TAPER_FULL_C
            ),
            cold_taper_min_factor=_entry_value(
                entry, CONF_COLD_TAPER_MIN_FACTOR, DEFAULT_COLD_TAPER_MIN_FACTOR
            ),
            precharge_max_boost_c=_entry_value(
                entry, CONF_PRECHARGE_MAX_BOOST_C, DEFAULT_PRECHARGE_MAX_BOOST_C
            ),
        )

    def _update_autotune(
        self, indoor_temp_c: float | None, raw_outdoor_temp_c: float
    ) -> None:
        """Re-derive the controller coefficients from the RC model's current
        beliefs, storing the result on `self.autotune_result`.

        Runs in BOTH tuning modes — the derived values are diagnostic output in
        Manual mode and control input in Auto mode, and publishing them either
        way is what lets a user watch derived-vs-manual for a season before
        committing. Must be called BEFORE `_params()` is used for this cycle.

        Reads the PREVIOUS cycle's `rc_result` rather than this one's, because
        the RC estimator is stepped after the heuristic has run (it needs the
        applied compensation delta as its control regressor). The resulting one
        cycle of staleness is immaterial against time constants measured in
        hours, and it avoids an ordering cycle between the two models.

        Wrapped in the same never-break-the-output contract as the RC and MPC
        updates: on any failure the result is left as-is and the heuristic falls
        back to whatever it had, which in the worst case is the manual
        coefficients.
        """
        try:
            rc_result = self.rc_result
            if rc_result is None:
                self.autotune_result = None
                return
            rc_config = self._rc_config()
            manual_k_indoor, manual_k_wind, manual_k_sun, manual_k_price = (
                self.manual_k_values()
            )
            tier = resolve_price_tier(self.price_comfort_tier)
            # The manual taper this cycle, both as the blend's starting point
            # and as the fallback when the derivation can't run.
            manual_taper = cold_taper_factor(
                raw_outdoor_temp_c,
                _entry_value(
                    self.entry, CONF_COLD_TAPER_START_C, DEFAULT_COLD_TAPER_START_C
                ),
                _entry_value(
                    self.entry, CONF_COLD_TAPER_FULL_C, DEFAULT_COLD_TAPER_FULL_C
                ),
                _entry_value(
                    self.entry,
                    CONF_COLD_TAPER_MIN_FACTOR,
                    DEFAULT_COLD_TAPER_MIN_FACTOR,
                ),
            )
            self.autotune_result = autotune_derive(
                AutotuneInputs(
                    theta_env=rc_result.theta_env,
                    theta_gain=rc_result.theta_gain,
                    theta_solar=rc_result.theta_solar,
                    theta_wind=rc_result.theta_wind,
                    gain_modeled=rc_result.gain_modeled,
                    params_pinned=self._rc_params_pinned(
                        rc_result, rc_config.enable_wind, rc_config.enable_solar
                    ),
                    accepted_samples=rc_result.accepted_samples,
                    indoor_temp_c=indoor_temp_c,
                    outdoor_temp_c=raw_outdoor_temp_c,
                    heating_type=_entry_value(
                        self.entry, CONF_HEATING_TYPE, DEFAULT_HEATING_TYPE
                    ),
                    wind_reference_ms=rc_config.wind_reference_ms,
                    max_heating_delta_c=_entry_value(
                        self.entry,
                        CONF_MPC_MAX_HEATING_DELTA_C,
                        DEFAULT_MPC_MAX_HEATING_DELTA_C,
                    ),
                    tier_max_sag_c=tier.max_sag_c,
                    enable_solar=self.solar_input_enabled,
                    enable_wind=rc_config.enable_wind,
                    manual_k_indoor=manual_k_indoor,
                    manual_k_wind=manual_k_wind,
                    manual_k_sun=manual_k_sun,
                    manual_k_price=manual_k_price,
                    manual_cold_taper_factor=manual_taper,
                )
            )
            _LOGGER.debug("Auto-tune: %s", self.autotune_result.reason)
        except Exception as err:  # noqa: BLE001 - never break output over tuning
            _LOGGER.warning("Auto-tune derivation failed (ignored): %s", err)

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
        if not self.weather_needed:
            # Both weather-derived inputs are switched off, so there is nothing
            # here to fetch. Returning the same soft-degraded tuple an
            # unavailable entity produces keeps every downstream consumer on one
            # code path; the heuristic zeroes both terms via its own
            # enable_*_input flags regardless of these values.
            return 0.0, False, None, False

        weather_entity_id = _entry_value(self.entry, CONF_WEATHER_ENTITY, None)
        if not weather_entity_id:
            _LOGGER.debug(
                "No weather entity configured; wind/sun terms contribute 0 this cycle"
            )
            return 0.0, False, None, False
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
        # Only warn about a field the user actually asked to use — a missing
        # wind forecast is not a problem worth logging every cycle when the wind
        # term is switched off.
        if not wind_data_available and self.wind_input_enabled:
            _LOGGER.warning(
                "Could not retrieve a wind forecast for %s; wind adjustment "
                "will contribute 0 this cycle",
                weather_entity_id,
            )
        if not cloud_data_available and self.solar_input_enabled:
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

    def _read_power_w(self) -> tuple[float | None, bool]:
        """Return (power_w, power_data_available) for the optional heat-pump
        power sensor. Soft-degrades like the price entity: unconfigured or
        unavailable just means no power/cost figure this cycle, nothing else
        is affected."""
        entity_id = _entry_value(self.entry, CONF_POWER_SENSOR, None)
        if not entity_id:
            return None, False
        state = self.hass.states.get(entity_id)
        if not _state_is_usable(state):
            _LOGGER.warning(
                "Power sensor %s is unavailable; power/cost logging will skip this cycle",
                entity_id,
            )
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
        """Coarse energy/cost estimate for one logged cycle: `last_power_w`
        held constant (left-rectangle) over the time since the previous
        *logged* cycle. Adequate for an offline cost trend, not billing-grade
        metering. Returns (None, None) when the power sensor is unavailable
        or this is the first logged cycle (no elapsed time to integrate over
        yet — also naturally covers logging having just been re-enabled,
        which resets `_energy_last_monotonic` via a full entry reload).

        NOTE: on installs where the power sensor is shared with hot water
        production, this energy/cost figure is NOT attributable to space
        heating alone — see README.
        """
        now = time.monotonic()
        last = self._energy_last_monotonic
        self._energy_last_monotonic = now
        if not power_ok or last is None:
            return None, None
        dt_h = (now - last) / 3600.0
        energy_kwh = self.last_power_w / 1000.0 * dt_h
        cost = energy_kwh * current_price if (price_ok and current_price is not None) else None
        return energy_kwh, cost

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
        price_forecast = self._price_forecast_offsets()
        self._last_price_forecast = price_forecast
        power_w, power_ok = self._read_power_w()
        self.last_power_w = power_w
        self.last_power_data_available = power_ok

        # Re-derive the coefficients from the RC model before building params,
        # since `_params()` reads the result in Auto mode.
        self._update_autotune(indoor_temp_c, raw_outdoor_temp_c)

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
            price_forecast=price_forecast,
        )
        result = compute(inputs, self._params())

        # Shadow mode: feed the RC estimator but never let it affect `result`.
        self._update_rc_shadow_model(result)

        # Advisory mode: compute an MPC plan from the RC model's current
        # beliefs, again without ever affecting `result`.
        await self._update_mpc_shadow(result)

        # Optional: push the same value the main sensor publishes to another
        # integration's number entity (e.g. a heat pump's virtual outdoor-temp
        # input). Unlike the RC/MPC updates above, this is a real side effect
        # when configured — but it never affects `result` itself, and is a
        # no-op when unconfigured (the default).
        await self._async_push_output_number(result)

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
        rc_config = self._rc_config()
        return rc_serialize_state(
            self._rc_state,
            enable_solar=rc_config.enable_solar,
            enable_wind=rc_config.enable_wind,
        )

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
        rc_config = self._rc_config()
        restored = rc_deserialize_state(
            data,
            enable_solar=rc_config.enable_solar,
            enable_wind=rc_config.enable_wind,
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
            await self._rc_store.async_save(self._serialize_rc_state())
        except Exception as err:  # noqa: BLE001 - never break unload over shadow state
            _LOGGER.warning("Could not persist RC shadow state on unload (ignored): %s", err)

    # --- Phase 3 MPC (shadow/advisory) ---------------------------------------

    @staticmethod
    def _rc_params_pinned(
        rc_result: RCModelResult, enable_wind: bool, enable_solar: bool = True
    ) -> bool:
        """Whether any RC parameter currently sits at (within a tiny tolerance
        of) one of its physical clip bounds — a sign the estimator hit a
        guardrail rather than converging, which the MPC trust gate treats as
        "not plausible yet". Checked here (not in the pure mpc module) so mpc.py
        need not know rc_model's bound constants.

        `enable_wind`/`enable_solar` are passed explicitly rather than inferred
        from `theta_* != 0.0`: THETA_WIND_MIN and THETA_SOLAR_MIN are both 0.0,
        so a term genuinely clipped down to its floor by real data would be
        indistinguishable from one still sitting untouched at its cold-start
        prior — inferring from the value alone would silently miss that real
        clip event. The flags also matter in the opposite direction: a disabled
        dimension is not estimated at all and `RCModelResult` reports a
        hardcoded 0.0 for it, which would otherwise read as permanently pinned
        at its lower bound and wedge every trust gate shut. For the same reason
        the gain bound is only checked once the gain dimension actually exists
        (`rc_result.gain_modeled`): before then `theta_gain` is a
        not-yet-modeled 0.0, which is not a real clip event and must not be
        treated as one (the MPC trust gate reports "not yet excited"
        separately).
        """
        tol = 1e-6
        checks = [(rc_result.theta_env, THETA_ENV_MIN, THETA_ENV_MAX)]
        if enable_solar:
            checks.append((rc_result.theta_solar, THETA_SOLAR_MIN, THETA_SOLAR_MAX))
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
            params_pinned=self._rc_params_pinned(
                rc_result, rc_config.enable_wind, rc_config.enable_solar
            ),
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
        entries = self._read_price_entries()
        if not entries:
            return [flat] * steps, 0
        return self._align_series(entries, dt_util.now(), steps, step_hours)

    def _read_price_entries(self) -> list[tuple[datetime, float]]:
        """Parse the Nordpool sensor's `raw_today` / `raw_tomorrow` attributes
        into (local start datetime, price) entries, shared by the MPC sampler
        and the heuristic's forecast offsets. Empty when no price entity is
        configured, it's unavailable, or those attributes are absent."""
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
        """The day-ahead price as (hours_from_now, price) pairs for the
        heuristic's relative-to-day band and lookahead pre-braking. None when
        unavailable, so the heuristic falls back to absolute-threshold,
        current-price-only behavior."""
        entries = self._read_price_entries()
        if not entries:
            return None
        now = dt_util.now()
        return tuple(
            ((start - now).total_seconds() / 3600.0, price)
            for start, price in sorted(entries, key=lambda e: e[0])
        )

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

    # --- Optional output push (HA number entity, or OhmOnWifi direct) --------

    async def _async_push_output_number(self, result: HeuristicResult) -> None:
        """Push the same value the main sensor publishes to whichever
        external target(s) are configured, so the computed result can
        actually drive a device instead of only being visible as a HA
        sensor. The two channels are independent, not alternatives — if both
        are set, both are pushed to every cycle:

        - Direct: an OhmOnWifi/Ohmigo device's own local HTTP API
          (`ohmonwifi_host`, e.g. "ohmonwifi.local") — no HA entity in between.
        - Indirect: another integration's `number.*` entity
          (`output_number_entity_id`, e.g. `number.nibe_ohmigo_temperature`
          if OhmOnWifi is set up as a HA number instead).

        Either way, this mirrors `CompensatedOutdoorTempSensor.native_value`
        exactly (raw outdoor temp while in learn mode, compensated once the
        activation switch is on), so flipping that switch behaves
        consistently everywhere the result is exposed. Each channel skips
        its own push when the value hasn't moved beyond
        `OUTPUT_NUMBER_WRITE_TOLERANCE_C` since *that channel's* last
        successful one, and each is strictly best-effort and independent of
        the other: a failure on one (device/entity unreachable or gone,
        wrong domain, outside a target's configured min/max) is logged and
        swallowed, never affecting the other channel or the real output.
        """
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
            await self._async_push_output_number_entity(entity_id, value)

    async def _async_push_ohmonwifi(self, host: str, value: float) -> None:
        """GET `http://<host>/AT/?T=<value>` — OhmOnWifi's own local API call
        to set its emulated resistance output to whatever its temperature/
        resistance conversion table maps `value` to, i.e. makes the connected
        heat pump see `value` as its outdoor temperature. Plain HTTP, no
        authentication, per Ohmigo's published API doc. Best-effort: any
        failure (device unreachable, DNS/mDNS resolution failure, timeout) is
        logged and swallowed.
        """
        if (
            self._last_ohmonwifi_value_c is not None
            and abs(value - self._last_ohmonwifi_value_c) < OUTPUT_NUMBER_WRITE_TOLERANCE_C
        ):
            return
        session = async_get_clientsession(self.hass)
        url = f"http://{host}/AT/"
        try:
            async with session.get(
                url,
                params={"T": f"{value:.1f}"},
                timeout=aiohttp.ClientTimeout(total=OHMONWIFI_REQUEST_TIMEOUT_SECONDS),
            ) as response:
                response.raise_for_status()
            self._last_ohmonwifi_value_c = value
        except Exception as err:  # noqa: BLE001 - best-effort push, never break output
            _LOGGER.warning(
                "Could not push compensated outdoor temperature to OhmOnWifi "
                "device %s (ignored): %s",
                host,
                err,
            )

    async def _async_push_output_number_entity(self, entity_id: str, value: float) -> None:
        """Push via a HA `number.set_value` service call. Best-effort: any
        failure (entity gone, wrong domain, outside the target's min/max) is
        logged and swallowed."""
        if (
            self._last_output_number_entity_value_c is not None
            and abs(value - self._last_output_number_entity_value_c)
            < OUTPUT_NUMBER_WRITE_TOLERANCE_C
        ):
            return
        try:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": entity_id, "value": value},
                blocking=True,
            )
            self._last_output_number_entity_value_c = value
        except Exception as err:  # noqa: BLE001 - best-effort push, never break output
            _LOGGER.warning(
                "Could not push compensated outdoor temperature to %s (ignored): %s",
                entity_id,
                err,
            )

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
            # Price-compensation v2 decision: enough to reconstruct offline why
            # it braked / pre-charged (or didn't) on this cycle.
            "price_comfort_tier": result.price_comfort_tier,
            "price_response": result.price_response,
            "price_shift_applied_c": result.price_shift_applied_c,
            "effective_indoor_target_c": result.effective_indoor_target_c,
            "price_adjustment_c": result.price_adjustment_c,
            "cold_taper_factor": result.cold_taper_factor,
            "allowed_sag_c": result.allowed_sag_c,
            "upcoming_spike_in_min": result.upcoming_spike_in_min,
            "precharge_active": result.precharge_active,
            "price_band_start": result.price_band_start,
            "price_band_full": result.price_band_full,
            "price_median": result.price_median,
        }
        cycle_energy_kwh, cycle_cost = self._cycle_energy_and_cost(
            self.last_power_data_available, result.current_price, result.price_data_available
        )
        record.update(
            {
                # Raw instantaneous reading (may include hot water — see README)
                # plus the coarse per-cycle energy/cost estimate derived from it.
                "power_w": self.last_power_w,
                "power_data_available": self.last_power_data_available,
                "cycle_energy_kwh": cycle_energy_kwh,
                "cycle_cost": cycle_cost,
            }
        )
        if self._last_price_forecast is not None:
            # The exact (hours_from_now, price) series the price decision above
            # was computed against — the heuristic analogue of the MPC forecast
            # snapshot below, needed to faithfully replay a past decision.
            record["price_forecast"] = [
                [round(h, 4), p] for h, p in self._last_price_forecast
            ]
        # Auto-tuning: what Manual would have used, what the model proposed, and
        # what was actually in force. The third is the one nothing else can
        # recover — in Auto mode the effective coefficients move every cycle and
        # are not inferable from the stored config, so without this an offline
        # replay cannot reconstruct what the controller was doing.
        record.update(
            autotune_log_fields(
                self.autotune_result, self.tuning_mode, self.manual_k_values()
            )
        )
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
