"""Unit tests for the pure RC / RLS shadow model.

rc_model.py has zero Home Assistant dependency, so it's loaded directly by
file path here (mirroring test_heuristic.py) rather than via
`custom_components.climate_optimizer`, which would pull in `homeassistant`
through the package's __init__.py.
"""

from __future__ import annotations

import importlib.util
import math
import random
import sys
from pathlib import Path

_RC_PATH = (
    Path(__file__).parent.parent
    / "custom_components"
    / "climate_optimizer"
    / "rc_model.py"
)
_spec = importlib.util.spec_from_file_location("rc_model", _RC_PATH)
rc_model = importlib.util.module_from_spec(_spec)
sys.modules["rc_model"] = rc_model
_spec.loader.exec_module(rc_model)

RCModelInputs = rc_model.RCModelInputs
RCModelConfig = rc_model.RCModelConfig
initial_state = rc_model.initial_state
step = rc_model.step

DT = 900.0  # 15 min, seconds
DT_H = DT / 3600.0


def make_inputs(**overrides) -> RCModelInputs:
    defaults = dict(
        indoor_temp_c=21.0,
        indoor_data_available=True,
        outdoor_temp_c=3.0,
        compensation_delta_c=-2.0,
        solar_effect=0.0,
        dt_seconds=DT,
    )
    defaults.update(overrides)
    return RCModelInputs(**defaults)


def _simulate_true_system(theta_true, n_cycles, noise_std=0.0, seed=1):
    """Generate a synthetic trajectory from a known linear RC system.

    Returns a list of (inputs_kwargs, next_indoor) style cycles already wired
    so that feeding the produced inputs into step() is self-consistent: each
    cycle's regressors (built from the PREVIOUS cycle's conditions) explain the
    realised indoor change. Drivers are independently randomised for good
    excitation / identifiability of all three parameters.
    """
    rng = random.Random(seed)
    theta_env, theta_gain, theta_solar = theta_true
    indoor = 21.0
    cycles = []
    for k in range(n_cycles):
        # Independently varied drivers -> the 3 regressors are well excited.
        outdoor = rng.uniform(-10.0, 10.0)
        u = rng.uniform(-4.0, 1.0)          # compensation delta (mostly cooling seen)
        solar = rng.uniform(0.0, 1.0)
        d_indoor = (
            theta_env * (outdoor - indoor) * DT_H
            + theta_gain * u * DT_H
            + theta_solar * solar * DT_H
        )
        if noise_std:
            d_indoor += rng.gauss(0.0, noise_std)
        next_indoor = indoor + d_indoor
        cycles.append(
            dict(
                indoor_temp_c=indoor,
                outdoor_temp_c=outdoor,
                compensation_delta_c=u,
                solar_effect=solar,
            )
        )
        indoor = next_indoor
    # Append the final indoor so the last cycle has a realised target.
    cycles.append(
        dict(
            indoor_temp_c=indoor,
            outdoor_temp_c=0.0,
            compensation_delta_c=0.0,
            solar_effect=0.0,
        )
    )
    return cycles


def _run(cycles, config=None):
    state = initial_state()
    last = None
    for c in cycles:
        state, last = step(state, make_inputs(**c), config or RCModelConfig())
    return state, last


def test_cold_start_first_sample_not_fitted():
    state = initial_state()
    new_state, result = step(state, make_inputs(), RCModelConfig())
    assert result.sample_accepted is False
    assert new_state.have_prev is True
    assert result.accepted_samples == 0
    assert "Cold start" in result.reason


def test_parameters_converge_to_known_truth_with_noise():
    theta_true = (0.05, -0.15, 0.40)  # tau=20h
    cycles = _simulate_true_system(theta_true, n_cycles=600, noise_std=0.01, seed=7)
    state, result = _run(cycles)

    assert result.accepted_samples > 500
    assert math.isclose(state.theta[0], theta_true[0], rel_tol=0.20)
    assert math.isclose(state.theta[1], theta_true[1], rel_tol=0.20)
    assert math.isclose(state.theta[2], theta_true[2], rel_tol=0.20)
    # Time constant should land near 20 h.
    assert 16.0 < result.time_constant_h < 25.0
    assert result.confidence == 1.0


def test_outlier_absolute_step_is_rejected_without_corrupting_fit():
    theta_true = (0.05, -0.15, 0.40)
    cycles = _simulate_true_system(theta_true, n_cycles=120, noise_std=0.005, seed=3)
    state = initial_state()
    result = None
    for c in cycles:
        state, result = step(state, make_inputs(**c), RCModelConfig())

    theta_before = state.theta
    rejected_before = state.rejected_samples

    # Inject a physically implausible +8 degC indoor jump (door/window/glitch).
    glitch_indoor = state.prev_indoor_temp_c + 8.0
    state, result = step(
        state,
        make_inputs(indoor_temp_c=glitch_indoor, outdoor_temp_c=0.0),
        RCModelConfig(),
    )
    assert result.sample_accepted is False
    assert state.rejected_samples == rejected_before + 1
    # Parameters untouched by the rejected sample.
    assert state.theta == theta_before
    assert "Implausible" in result.reason or "outlier" in result.reason


def test_adaptive_sigma_gate_rejects_statistical_outlier():
    theta_true = (0.05, -0.15, 0.40)
    cycles = _simulate_true_system(theta_true, n_cycles=200, noise_std=0.01, seed=11)
    state = initial_state()
    for c in cycles:
        state, _ = step(state, make_inputs(**c), RCModelConfig())

    assert state.accepted_samples >= rc_model.WARMUP_SAMPLES
    theta_before = state.theta
    rejected_before = state.rejected_samples

    # A within-abs-bound but statistically inconsistent jump (~3 degC where
    # typical steps are tiny) should trip the residual-sigma gate.
    outlier_indoor = state.prev_indoor_temp_c + 3.0
    state, result = step(
        state,
        make_inputs(indoor_temp_c=outlier_indoor, outdoor_temp_c=0.0,
                    compensation_delta_c=0.0, solar_effect=0.0),
        RCModelConfig(),
    )
    assert result.sample_accepted is False
    assert state.rejected_samples == rejected_before + 1
    assert state.theta == theta_before
    assert "sigma" in result.reason


def test_parameter_clipping_engages_and_holds_bounds():
    # Adversarial: drive the indoor temperature in the OPPOSITE direction to
    # what the envelope term implies, hard, to push theta_env negative and
    # theta_solar negative; clipping must hold both at their bounds.
    state = initial_state()
    config = RCModelConfig()
    indoor = 21.0
    result = None
    for k in range(300):
        # Outdoor much colder than indoor (envelope wants cooling), but we
        # force indoor to RISE strongly and steadily -> pushes theta_env < 0.
        outdoor = -20.0
        state, result = step(
            state,
            RCModelInputs(
                indoor_temp_c=indoor,
                indoor_data_available=True,
                outdoor_temp_c=outdoor,
                compensation_delta_c=0.0,
                solar_effect=1.0,          # positive solar but paired with...
                dt_seconds=DT,
            ),
            config,
        )
        indoor += 0.02  # steady rise despite cold outside and no heat proxy

    assert rc_model.THETA_ENV_MIN <= state.theta[0] <= rc_model.THETA_ENV_MAX
    assert rc_model.THETA_GAIN_MIN <= state.theta[1] <= rc_model.THETA_GAIN_MAX
    assert rc_model.THETA_SOLAR_MIN <= state.theta[2] <= rc_model.THETA_SOLAR_MAX
    assert state.clip_events > 0
    # All parameters remain finite.
    assert all(math.isfinite(v) for v in state.theta)


def test_long_run_is_numerically_stable():
    theta_true = (0.03, -0.10, 0.25)
    rng = random.Random(99)
    state = initial_state()
    config = RCModelConfig()
    indoor = 21.0
    result = None
    for k in range(2000):
        outdoor = rng.uniform(-15.0, 15.0)
        u = rng.uniform(-4.0, 2.0)
        solar = rng.uniform(0.0, 1.0)
        d = (
            theta_true[0] * (outdoor - indoor) * DT_H
            + theta_true[1] * u * DT_H
            + theta_true[2] * solar * DT_H
            + rng.gauss(0.0, 0.02)
        )
        state, result = step(
            state,
            RCModelInputs(
                indoor_temp_c=indoor,
                indoor_data_available=True,
                outdoor_temp_c=outdoor,
                compensation_delta_c=u,
                solar_effect=solar,
                dt_seconds=DT,
            ),
            config,
        )
        indoor += d

    assert all(math.isfinite(v) for v in state.theta)
    assert math.isfinite(result.cov_trace)
    assert result.cov_trace <= rc_model.P_TRACE_MAX * 1.0001
    assert result.cov_trace > 0
    assert math.isfinite(result.time_constant_h)
    # Covariance matrix stayed finite and positive on the diagonal.
    for i in range(3):
        assert math.isfinite(state.p_matrix[i][i])
        assert state.p_matrix[i][i] > 0


def test_indoor_unavailable_skips_update_and_holds_state():
    state = initial_state()
    state, _ = step(state, make_inputs(), RCModelConfig())  # cold-start anchor
    before = state
    state, result = step(
        state,
        make_inputs(indoor_temp_c=None, indoor_data_available=False),
        RCModelConfig(),
    )
    assert result.sample_accepted is False
    assert "unavailable" in result.reason
    # prev not advanced, params unchanged.
    assert state.theta == before.theta
    assert state.prev_indoor_temp_c == before.prev_indoor_temp_c


def test_large_dt_gap_rejected_and_reanchored():
    theta_true = (0.05, -0.15, 0.40)
    cycles = _simulate_true_system(theta_true, n_cycles=60, noise_std=0.005, seed=5)
    state = initial_state()
    for c in cycles:
        state, _ = step(state, make_inputs(**c), RCModelConfig())
    rejected_before = state.rejected_samples
    theta_before = state.theta

    # A 12 h gap (e.g. HA was down) exceeds MAX_DT_SECONDS.
    state, result = step(
        state,
        make_inputs(dt_seconds=12 * 3600.0),
        RCModelConfig(),
    )
    assert result.sample_accepted is False
    assert state.rejected_samples == rejected_before + 1
    assert state.theta == theta_before
    assert "gap" in result.reason or "discontinuity" in result.reason
    # Re-anchored so the next normal cycle can fit again.
    assert state.have_prev is True


def test_prediction_error_populated_after_two_accepted_cycles():
    theta_true = (0.05, -0.15, 0.40)
    cycles = _simulate_true_system(theta_true, n_cycles=10, noise_std=0.0, seed=2)
    state = initial_state()
    results = []
    for c in cycles:
        state, r = step(state, make_inputs(**c), RCModelConfig())
        results.append(r)
    # After the estimator has produced a prediction, later cycles score it.
    later = [r for r in results if r.prediction_error_c is not None]
    assert later, "expected at least one scored prediction error"
    # With zero noise and a self-consistent system the prediction error is tiny.
    assert abs(later[-1].prediction_error_c) < 0.5


def test_non_finite_input_rejected():
    state = initial_state()
    state, _ = step(state, make_inputs(), RCModelConfig())
    state, result = step(
        state, make_inputs(outdoor_temp_c=float("nan")), RCModelConfig()
    )
    assert result.sample_accepted is False
    assert "Non-finite" in result.reason
