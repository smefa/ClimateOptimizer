"""Config-entry diagnostics: one-click full state dump.

Surfaced in Home Assistant as the "Download diagnostics" button on the
integration's device/entry page. HA discovers this module by name — no platform
registration in __init__.py is needed.

Why this exists on top of the live sensors and the opt-in JSONL log: those two
answer different questions. The sensors show the latest value of a handful of
things; the JSONL log is a long, opt-in, offline-analysis time series that most
users will never have enabled. Neither gives a complete cross-sectional snapshot
of *everything at once* when something looks wrong right now — which config was
actually in force, what all four models currently believe, and (crucially) the
raw RLS covariance matrix, which is the single most useful artefact for
diagnosing an estimator that has stopped converging and is not exposed anywhere
else at all.

Redaction: the two output-push targets are removed. Neither is a credential
(there are none in this integration), but a hostname/IP and the entity id of a
device register describe someone's local network and hardware, and diagnostics
files routinely get pasted into public issue trackers. Everything else is kept
deliberately — entity ids of *source* sensors are frequently what the problem
turns out to be, so redacting them would defeat the purpose.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import ClimateOptimizerConfigEntry
from .autotune import (
    CLOSED_LOOP_SPEEDUP,
    MIN_GAIN_MAGNITUDE,
    WARMUP_SAMPLES as AUTOTUNE_WARMUP_SAMPLES,
    emitter_tau_floor_h,
)
from .const import (
    CONF_HEATING_TYPE,
    CONF_OHMONWIFI_HOST,
    CONF_OUTPUT_NUMBER_ENTITY,
    DEFAULT_HEATING_TYPE,
)

TO_REDACT = {CONF_OHMONWIFI_HOST, CONF_OUTPUT_NUMBER_ENTITY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ClimateOptimizerConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    rc_config = coordinator._rc_config()
    rc_state = coordinator.rc_estimator_state
    heating_type = entry.options.get(
        CONF_HEATING_TYPE, entry.data.get(CONF_HEATING_TYPE, DEFAULT_HEATING_TYPE)
    )
    manual_k_indoor, manual_k_wind, manual_k_sun, manual_k_price = (
        coordinator.manual_k_values()
    )
    last_error = coordinator.last_exception

    return {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        # Live values that are NOT config — each is backed by a restored entity
        # rather than the config entry, so reading the options alone would give
        # a misleading picture of what the controller was actually doing.
        "runtime": {
            "is_active": coordinator.is_active,
            "tuning_mode": coordinator.tuning_mode,
            "indoor_target_c": coordinator.indoor_target_c,
            "price_comfort_tier": coordinator.price_comfort_tier,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "last_update_success": coordinator.last_update_success,
            "last_error": str(last_error) if last_error else None,
            "solar_input_enabled": coordinator.solar_input_enabled,
            "wind_input_enabled": coordinator.wind_input_enabled,
            "weather_needed": coordinator.weather_needed,
            "data_logging_enabled": coordinator.data_logging_enabled,
            "data_log_path": coordinator.data_log_path,
            "output_push_configured": {
                # Whether each channel is set, without disclosing the target.
                "ohmonwifi": coordinator.ohmonwifi_host is not None,
                "number_entity": coordinator.output_number_entity_id is not None,
            },
        },
        "heuristic_result": (
            asdict(coordinator.data) if coordinator.data is not None else None
        ),
        "rc": {
            "config": asdict(rc_config),
            "result": (
                asdict(coordinator.rc_result)
                if coordinator.rc_result is not None
                else None
            ),
            # The raw estimator internals. `p_matrix` in particular is exposed
            # nowhere else and is what distinguishes "still converging" from
            # "covariance has wound up and the fit has stopped responding" —
            # the failure mode rc_model.py's trace cap exists to bound.
            "estimator_state": {
                "theta": list(rc_state.theta),
                "p_matrix": [list(row) for row in rc_state.p_matrix],
                "has_gain": rc_state.has_gain,
                "accepted_samples": rc_state.accepted_samples,
                "rejected_samples": rc_state.rejected_samples,
                "clip_events": rc_state.clip_events,
                "resid_var": rc_state.resid_var,
                "have_prev": rc_state.have_prev,
            },
        },
        "autotune": {
            # The constants the derivation actually used, resolved rather than
            # named, so a report is self-contained without cross-referencing
            # this version's source.
            "config": {
                "heating_type": heating_type,
                "emitter_tau_floor_h": emitter_tau_floor_h(heating_type),
                "closed_loop_speedup": CLOSED_LOOP_SPEEDUP,
                "min_gain_magnitude": MIN_GAIN_MAGNITUDE,
                "warmup_samples": AUTOTUNE_WARMUP_SAMPLES,
            },
            "manual_coefficients": {
                "k_indoor": manual_k_indoor,
                "k_wind": manual_k_wind,
                "k_sun": manual_k_sun,
                "k_price": manual_k_price,
            },
            "result": (
                asdict(coordinator.autotune_result)
                if coordinator.autotune_result is not None
                else None
            ),
        },
        "mpc": {
            "config": asdict(coordinator._mpc_config()),
            "result": (
                asdict(coordinator.mpc_result)
                if coordinator.mpc_result is not None
                else None
            ),
        },
    }
