"""Unit tests for the pure RC-state (de)serialization layer, rc_store.py.

Like test_rc_model.py, these load the modules directly by file path so the
suite needs no Home Assistant install (CI only pip-installs pytest). rc_model
is loaded first and registered in sys.modules as "rc_model" so rc_store's
import shim (`from .rc_model import ...` -> fallback `from rc_model import ...`)
resolves to it without any package context. rc_store.py itself imports NO
homeassistant module, on purpose, which is exactly what makes this possible.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

_CC = (
    Path(__file__).parent.parent
    / "custom_components"
    / "climate_optimizer"
)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _CC / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Order matters: rc_store's import shim resolves "rc_model" from sys.modules.
rc_model = _load("rc_model", "rc_model.py")
rc_store = _load("rc_store", "rc_store.py")

initial_state = rc_model.initial_state
step = rc_model.step
RCModelConfig = rc_model.RCModelConfig
RCModelInputs = rc_model.RCModelInputs
RCModelState = rc_model.RCModelState

serialize_state = rc_store.serialize_state
deserialize_state = rc_store.deserialize_state

DT = 900.0  # 15 min, seconds


def _make_inputs(**overrides) -> RCModelInputs:
    defaults = dict(
        indoor_temp_c=21.0,
        indoor_data_available=True,
        outdoor_temp_c=3.0,
        compensation_delta_c=-2.0,
        solar_effect=0.3,
        wind_speed_ms=4.0,
        dt_seconds=DT,
    )
    defaults.update(overrides)
    return RCModelInputs(**defaults)


def _matured_state(enable_wind: bool, n_cycles: int = 8) -> RCModelState:
    """Drive the estimator a handful of cycles so the state under test is
    non-trivial (moved theta, non-diagonal P, populated prev_* fields,
    counters advanced) rather than the pristine cold-start prior."""
    config = RCModelConfig(enable_wind=enable_wind)
    state = initial_state(enable_wind=enable_wind)
    indoor = 21.0
    for k in range(n_cycles):
        indoor += 0.05 * (k % 3 - 1)  # small varied indoor movement
        state, _ = step(
            state,
            _make_inputs(
                indoor_temp_c=indoor,
                outdoor_temp_c=2.0 + 0.5 * k,
                compensation_delta_c=-1.5 + 0.2 * k,
                solar_effect=0.1 * (k % 4),
                wind_speed_ms=3.0 + 0.3 * k,
            ),
            config,
        )
    return state


def _roundtrip(state: RCModelState, *, enable_wind: bool) -> RCModelState | None:
    """serialize -> JSON text -> parse -> deserialize, mimicking what the HA
    Store actually does on disk (and proving the payload is JSON-safe)."""
    payload = json.loads(json.dumps(serialize_state(state)))
    return deserialize_state(payload, enable_wind=enable_wind)


def _assert_states_equal(a: RCModelState, b: RCModelState) -> None:
    assert a.theta == b.theta
    assert a.p_matrix == b.p_matrix
    assert a.accepted_samples == b.accepted_samples
    assert a.rejected_samples == b.rejected_samples
    assert a.clip_events == b.clip_events
    assert a.resid_var == b.resid_var
    assert a.have_prev == b.have_prev
    assert a.prev_indoor_temp_c == b.prev_indoor_temp_c
    assert a.prev_outdoor_temp_c == b.prev_outdoor_temp_c
    assert a.prev_u_c == b.prev_u_c
    assert a.prev_solar == b.prev_solar
    assert a.prev_wind_speed_ms == b.prev_wind_speed_ms
    assert a.prev_predicted_next_indoor_c == b.prev_predicted_next_indoor_c


# --- round trips -------------------------------------------------------------


def test_roundtrip_cold_start_no_gain_2d():
    # Cold start with wind off is now 2-dim ([env, solar]); has_gain False must
    # round-trip so a fresh estimator is persisted/restored faithfully.
    state = initial_state(enable_wind=False)
    assert len(state.theta) == 2 and state.has_gain is False
    restored = _roundtrip(state, enable_wind=False)
    assert restored is not None
    assert restored.has_gain is False
    _assert_states_equal(state, restored)


def test_roundtrip_matured_3d():
    state = _matured_state(enable_wind=False)
    assert state.accepted_samples > 0  # sanity: state actually evolved
    restored = _roundtrip(state, enable_wind=False)
    assert restored is not None
    _assert_states_equal(state, restored)


def test_roundtrip_matured_4d_wind():
    state = _matured_state(enable_wind=True)
    assert len(state.theta) == 4
    restored = _roundtrip(state, enable_wind=True)
    assert restored is not None
    _assert_states_equal(state, restored)


def test_serialized_payload_is_json_and_versioned():
    state = _matured_state(enable_wind=False)
    payload = serialize_state(state)
    # Must survive json.dumps unchanged (no tuples/sets/etc.).
    assert json.loads(json.dumps(payload)) == payload
    assert payload["schema_version"] == rc_store.STATE_SCHEMA_VERSION
    assert payload["model_version"] == rc_model.MODEL_VERSION
    assert payload["n_params"] == 3


# --- dimensionality mismatch (the explicit safety requirement) ---------------


def test_wind_state_rejected_by_no_wind_estimator():
    state = _matured_state(enable_wind=True)  # 4-dim
    payload = json.loads(json.dumps(serialize_state(state)))
    assert deserialize_state(payload, enable_wind=False) is None


def test_no_wind_state_rejected_by_wind_estimator():
    state = _matured_state(enable_wind=False)  # 3-dim
    payload = json.loads(json.dumps(serialize_state(state)))
    assert deserialize_state(payload, enable_wind=True) is None


# --- lazy gain dimension: has_gain flag + same-length disambiguation ----------


def test_roundtrip_post_expansion_preserves_has_gain():
    # A matured no-wind state has had the gain dimension added ([env, solar,
    # gain], has_gain True); the flag and the 3-dim state must round-trip.
    state = _matured_state(enable_wind=False)
    assert len(state.theta) == 3 and state.has_gain is True
    payload = serialize_state(state)
    assert payload["has_gain"] is True
    restored = _roundtrip(state, enable_wind=False)
    assert restored is not None
    assert restored.has_gain is True
    _assert_states_equal(state, restored)


def test_has_gain_disambiguates_same_length_states():
    # The crux: two DIFFERENT length-3 layouts exist. Length alone cannot tell
    # them apart; enable_wind + has_gain together must.
    wind_no_gain = initial_state(enable_wind=True)      # [env, solar, wind]
    assert len(wind_no_gain.theta) == 3 and wind_no_gain.has_gain is False
    gain_no_wind = _matured_state(enable_wind=False)    # [env, solar, gain]
    assert len(gain_no_wind.theta) == 3 and gain_no_wind.has_gain is True

    p_wind = json.loads(json.dumps(serialize_state(wind_no_gain)))
    p_gain = json.loads(json.dumps(serialize_state(gain_no_wind)))
    # Both are length 3 but carry different has_gain flags.
    assert p_wind["n_params"] == p_gain["n_params"] == 3
    assert p_wind["has_gain"] is False and p_gain["has_gain"] is True

    # Each loads only into the estimator config it actually belongs to...
    assert deserialize_state(p_wind, enable_wind=True) is not None
    assert deserialize_state(p_gain, enable_wind=False) is not None
    # ...and is rejected by the other, despite the identical length.
    assert deserialize_state(p_wind, enable_wind=False) is None
    assert deserialize_state(p_gain, enable_wind=True) is None


def test_missing_has_gain_key_discarded():
    # A payload lacking the has_gain flag (e.g. a pre-v2 store that somehow
    # matched schema_version) cannot be disambiguated and must be discarded.
    payload = serialize_state(_matured_state(enable_wind=False))
    del payload["has_gain"]
    assert deserialize_state(payload, enable_wind=False) is None


def test_non_bool_has_gain_discarded():
    payload = serialize_state(_matured_state(enable_wind=False))
    payload["has_gain"] = "yes"
    assert deserialize_state(payload, enable_wind=False) is None


def test_pre_v2_payload_discarded():
    # A legacy v1 payload (old parameter order [env, gain, solar], schema v1,
    # no has_gain) must be discarded so the estimator cold-starts cleanly rather
    # than misinterpreting the old ordering.
    legacy = {
        "schema_version": 1,
        "model_version": rc_model.MODEL_VERSION,
        "n_params": 3,
        "theta": [0.033, -0.1, 0.2],   # old order: env, gain, solar
        "p_matrix": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        "accepted_samples": 100,
        "rejected_samples": 0,
        "clip_events": 0,
        "resid_var": 0.01,
        "have_prev": True,
        "prev_indoor_temp_c": 21.0,
        "prev_outdoor_temp_c": 3.0,
        "prev_u_c": -1.0,
        "prev_solar": 0.3,
        "prev_wind_speed_ms": 4.0,
        "prev_predicted_next_indoor_c": 21.0,
    }
    assert deserialize_state(legacy, enable_wind=False) is None


# --- version / schema gating -------------------------------------------------


def test_schema_version_mismatch_discarded():
    payload = serialize_state(initial_state(enable_wind=False))
    payload["schema_version"] = rc_store.STATE_SCHEMA_VERSION + 1
    assert deserialize_state(payload, enable_wind=False) is None


def test_model_version_mismatch_discarded():
    payload = serialize_state(initial_state(enable_wind=False))
    payload["model_version"] = "some_other_model_vNN"
    assert deserialize_state(payload, enable_wind=False) is None


# --- corruption / structural robustness --------------------------------------


def test_non_dict_discarded():
    assert deserialize_state(None, enable_wind=False) is None
    assert deserialize_state([1, 2, 3], enable_wind=False) is None
    assert deserialize_state("garbage", enable_wind=False) is None


def test_missing_key_discarded():
    payload = serialize_state(initial_state(enable_wind=False))
    del payload["resid_var"]
    assert deserialize_state(payload, enable_wind=False) is None


def test_wrong_theta_length_discarded():
    # A matured no-wind state is 3-dim ([env, solar, gain]); n_params says 3 but
    # theta is truncated to 2 entries -> the shape check must catch it.
    payload = serialize_state(_matured_state(enable_wind=False))
    assert payload["n_params"] == 3
    payload["theta"] = payload["theta"][:2]
    assert deserialize_state(payload, enable_wind=False) is None


def test_ragged_p_matrix_discarded():
    payload = serialize_state(_matured_state(enable_wind=False))
    payload["p_matrix"][0] = payload["p_matrix"][0][:2]  # ragged row
    assert deserialize_state(payload, enable_wind=False) is None


def test_non_finite_theta_discarded():
    payload = serialize_state(initial_state(enable_wind=False))
    # NaN/inf are not valid JSON per spec, but a hand-corrupted store could
    # still contain them; deserialize must reject rather than resurrect them.
    payload["theta"][1] = float("nan")
    assert deserialize_state(payload, enable_wind=False) is None
    payload["theta"][1] = float("inf")
    assert deserialize_state(payload, enable_wind=False) is None


def test_non_finite_prev_slot_discarded():
    state = _matured_state(enable_wind=False)
    payload = serialize_state(state)
    payload["prev_indoor_temp_c"] = float("nan")
    assert deserialize_state(payload, enable_wind=False) is None


def test_none_prev_slots_are_valid():
    # Cold-start has have_prev False and all prev_* None; that must round-trip.
    state = initial_state(enable_wind=False)
    assert state.have_prev is False
    restored = _roundtrip(state, enable_wind=False)
    assert restored is not None
    assert restored.have_prev is False
    assert restored.prev_indoor_temp_c is None


def test_restored_state_resumes_estimation():
    """A restored state must be a drop-in for the live estimator: feeding it
    another cycle produces the same result as if it had never been persisted."""
    config = RCModelConfig(enable_wind=False)
    state = _matured_state(enable_wind=False)
    nxt = _make_inputs(indoor_temp_c=21.2, outdoor_temp_c=5.0)

    direct_state, direct_result = step(state, nxt, config)

    restored = _roundtrip(state, enable_wind=False)
    assert restored is not None
    resumed_state, resumed_result = step(restored, nxt, config)

    _assert_states_equal(direct_state, resumed_state)
    assert direct_result.theta_gain == resumed_result.theta_gain
    assert direct_result.accepted_samples == resumed_result.accepted_samples
    assert math.isclose(
        direct_result.time_constant_h, resumed_result.time_constant_h, rel_tol=0.0
    )
