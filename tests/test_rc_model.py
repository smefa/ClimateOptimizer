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
        wind_speed_ms=0.0,
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
    config = config or RCModelConfig()
    state = initial_state(enable_wind=config.enable_wind)
    last = None
    for c in cycles:
        state, last = step(state, make_inputs(**c), config)
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
    # Assert via the named result fields (ordering-independent): the synthetic
    # drivers include nonzero compensation deltas, so the gain dimension gets
    # added and all three parameters converge to truth.
    assert result.gain_modeled is True
    assert math.isclose(result.theta_env, theta_true[0], rel_tol=0.20)
    assert math.isclose(result.theta_gain, theta_true[1], rel_tol=0.20)
    assert math.isclose(result.theta_solar, theta_true[2], rel_tol=0.20)
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
                wind_speed_ms=0.0,
                dt_seconds=DT,
            ),
            config,
        )
        indoor += 0.02  # steady rise despite cold outside and no heat proxy

    # compensation_delta_c is 0.0 throughout, so the gain dimension is never
    # excited and never added: env and solar are clipped to their bounds, and
    # gain is honestly reported as not-yet-modeled rather than a clipped 0.0.
    assert result.gain_modeled is False
    assert rc_model.THETA_ENV_MIN <= result.theta_env <= rc_model.THETA_ENV_MAX
    assert rc_model.THETA_SOLAR_MIN <= result.theta_solar <= rc_model.THETA_SOLAR_MAX
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
                wind_speed_ms=0.0,
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


def _simulate_true_system_wind(theta_true, n_cycles, wind_fn, noise_std=0.0, seed=1):
    """Like _simulate_true_system but with a 4th (wind) term and a caller-
    supplied wind_fn(rng) -> wind_speed_ms, so tests can control whether wind
    is independently excited (identifiable) or collinear with cold (not).
    """
    rng = random.Random(seed)
    theta_env, theta_gain, theta_solar, theta_wind = theta_true
    indoor = 21.0
    cycles = []
    for k in range(n_cycles):
        outdoor = rng.uniform(-10.0, 10.0)
        u = rng.uniform(-4.0, 1.0)
        solar = rng.uniform(0.0, 1.0)
        wind = wind_fn(rng, outdoor)
        d_indoor = (
            theta_env * (outdoor - indoor) * DT_H
            + theta_gain * u * DT_H
            + theta_solar * solar * DT_H
            + theta_wind * (outdoor - indoor) * (wind / rc_model.DEFAULT_WIND_REFERENCE_MS) * DT_H
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
                wind_speed_ms=wind,
            )
        )
        indoor = next_indoor
    cycles.append(
        dict(
            indoor_temp_c=indoor,
            outdoor_temp_c=0.0,
            compensation_delta_c=0.0,
            solar_effect=0.0,
            wind_speed_ms=0.0,
        )
    )
    return cycles


def test_wind_disabled_cold_start_is_two_dimensional():
    # Cold start with wind off no longer carries a gain dimension either: the
    # estimator starts at [env, solar] and adds gain lazily on first excitation.
    state = initial_state()  # default enable_wind=False
    assert len(state.theta) == 2
    assert len(state.p_matrix) == 2
    assert state.has_gain is False


def test_wind_enabled_cold_start_is_three_dimensional_no_gain():
    # Wind on, gain not yet added: [env, solar, wind].
    state = initial_state(enable_wind=True)
    assert len(state.theta) == 3
    assert len(state.p_matrix) == 3
    assert state.has_gain is False


def test_wind_disabled_result_always_reports_zero_wind_gain():
    theta_true = (0.05, -0.15, 0.40)
    cycles = _simulate_true_system(theta_true, n_cycles=60, noise_std=0.01, seed=8)
    state = initial_state()
    result = None
    for c in cycles:
        # Even though wind_speed_ms defaults to 0.0 via make_inputs, feed some
        # nonzero wind to prove it's genuinely ignored when disabled, not just
        # coincidentally always zero in the input.
        state, result = step(
            state, make_inputs(wind_speed_ms=7.0, **c), RCModelConfig()
        )
    assert result.theta_wind == 0.0


def test_wind_param_converges_on_synthetic_windy_system():
    # A deliberately wind-sensitive (leaky) house: sizeable true theta_wind.
    theta_true = (0.05, -0.15, 0.40, 0.20)
    cycles = _simulate_true_system_wind(
        theta_true,
        n_cycles=800,
        wind_fn=lambda rng, outdoor: rng.uniform(0.0, 15.0),  # independent of outdoor
        noise_std=0.01,
        seed=21,
    )
    config = RCModelConfig(enable_wind=True)
    state, result = _run(cycles, config)

    assert result.accepted_samples > 700
    # Wind on and gain excited -> layout [env, solar, wind, gain], length 4.
    assert len(state.theta) == 4
    assert result.gain_modeled is True
    assert math.isclose(result.theta_env, theta_true[0], rel_tol=0.25)
    assert math.isclose(result.theta_wind, theta_true[3], rel_tol=0.35)
    assert result.theta_wind > 0.05  # clearly nonzero, not stuck at the prior


def test_wind_nonidentifiable_when_collinear_stays_bounded():
    # Wind strongly correlated with cold (mimicking real weather: cold days
    # tend to be windy days) but with a SMALL true wind effect. This is the
    # scenario the feature defaults to off for; with it explicitly enabled
    # anyway, the estimator must not manufacture a large spurious coefficient
    # from the collinearity - it should stay small/bounded, not blow up.
    theta_true = (0.05, -0.15, 0.40, 0.01)  # tiny true wind sensitivity

    def collinear_wind(rng, outdoor):
        # Colder outdoor -> windier, plus noise, clipped to a sane range.
        base = _clamp_local(-outdoor * 0.5, 0.0, 20.0)
        return _clamp_local(base + rng.gauss(0.0, 2.0), 0.0, 20.0)

    cycles = _simulate_true_system_wind(
        theta_true, n_cycles=500, wind_fn=collinear_wind, noise_std=0.02, seed=13
    )
    config = RCModelConfig(enable_wind=True)
    state, result = _run(cycles, config)

    assert math.isfinite(state.theta[3])
    # Should not have run away to anywhere near the upper bound from noise.
    assert state.theta[3] < 0.15


def _clamp_local(v, lo, hi):
    return max(lo, min(hi, v))


def test_wind_clip_nonnegative():
    # Adversarial: force indoor to warm up steadily despite cold+windy
    # conditions, which would require a negative theta_wind to "explain" -
    # must clip at THETA_WIND_MIN = 0.0, not go negative.
    state = initial_state(enable_wind=True)
    config = RCModelConfig(enable_wind=True)
    indoor = 21.0
    result = None
    for k in range(300):
        state, result = step(
            state,
            RCModelInputs(
                indoor_temp_c=indoor,
                indoor_data_available=True,
                outdoor_temp_c=-20.0,
                compensation_delta_c=0.0,
                solar_effect=0.0,
                wind_speed_ms=15.0,
                dt_seconds=DT,
            ),
            config,
        )
        indoor += 0.02  # steady rise despite cold + high wind
    # No compensation delta here, so gain is never added; wind sits at index 2.
    assert result.gain_modeled is False
    assert rc_model.THETA_WIND_MIN <= result.theta_wind <= rc_model.THETA_WIND_MAX
    assert math.isfinite(result.theta_wind)


def test_wind_clip_upper_bound():
    # Adversarial: indoor plummets much faster than the envelope+solar+gain
    # terms alone can explain, only when wind is high - pushes theta_wind up.
    state = initial_state(enable_wind=True)
    config = RCModelConfig(enable_wind=True)
    indoor = 21.0
    result = None
    for k in range(300):
        state, result = step(
            state,
            RCModelInputs(
                indoor_temp_c=indoor,
                indoor_data_available=True,
                outdoor_temp_c=-20.0,
                compensation_delta_c=0.0,
                solar_effect=0.0,
                wind_speed_ms=20.0,
                dt_seconds=DT,
            ),
            config,
        )
        indoor -= 2.0  # implausibly steep drop, far beyond what env alone gives
    assert result.gain_modeled is False
    assert result.theta_wind <= rc_model.THETA_WIND_MAX
    assert math.isfinite(result.theta_wind)


def test_wind_long_run_numerically_stable_with_storm_leverage():
    theta_true = (0.03, -0.10, 0.25, 0.15)
    rng = random.Random(77)

    def gusty_wind(rng, outdoor):
        # Occasional storm-magnitude gusts mixed into normal wind.
        if rng.random() < 0.05:
            return rng.uniform(15.0, 20.0)
        return rng.uniform(0.0, 6.0)

    state = initial_state(enable_wind=True)
    config = RCModelConfig(enable_wind=True)
    indoor = 21.0
    result = None
    for k in range(2000):
        outdoor = rng.uniform(-15.0, 15.0)
        u = rng.uniform(-4.0, 2.0)
        solar = rng.uniform(0.0, 1.0)
        wind = gusty_wind(rng, outdoor)
        d = (
            theta_true[0] * (outdoor - indoor) * DT_H
            + theta_true[1] * u * DT_H
            + theta_true[2] * solar * DT_H
            + theta_true[3] * (outdoor - indoor) * (wind / rc_model.DEFAULT_WIND_REFERENCE_MS) * DT_H
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
                wind_speed_ms=wind,
                dt_seconds=DT,
            ),
            config,
        )
        indoor += d

    assert all(math.isfinite(v) for v in state.theta)
    assert math.isfinite(result.cov_trace)
    assert result.cov_trace <= rc_model.P_TRACE_MAX * 1.0001
    assert result.cov_trace > 0
    for i in range(4):
        assert math.isfinite(state.p_matrix[i][i])
        assert state.p_matrix[i][i] > 0


def test_wind_scale_matching_at_reference_speed():
    # At wind == wind_reference_ms, the wind regressor's magnitude should be
    # comparable to the envelope regressor's (the whole point of normalising
    # by a reference speed rather than using raw m/s) - not orders of
    # magnitude larger.
    outdoor, indoor_t = -15.0, 21.0
    envelope_term = (outdoor - indoor_t) * DT_H
    wind_term = (
        (outdoor - indoor_t)
        * (rc_model.DEFAULT_WIND_REFERENCE_MS / rc_model.DEFAULT_WIND_REFERENCE_MS)
        * DT_H
    )
    assert wind_term == envelope_term  # identical by construction at v=v_ref

    # At a storm (20 m/s), it should be elevated but not wildly disproportionate.
    storm_term = (outdoor - indoor_t) * (20.0 / rc_model.DEFAULT_WIND_REFERENCE_MS) * DT_H
    assert abs(storm_term) <= abs(envelope_term) * 5


def test_non_finite_input_rejected():
    state = initial_state()
    state, _ = step(state, make_inputs(), RCModelConfig())
    state, result = step(
        state, make_inputs(outdoor_temp_c=float("nan")), RCModelConfig()
    )
    assert result.sample_accepted is False
    assert "Non-finite" in result.reason


# --- lazy heat-pump gain dimension -------------------------------------------


def _passive_cycles(n_cycles, seed=5):
    """Cycles with ZERO compensation delta (no heat-pump excitation) but well-
    excited envelope + solar, mimicking an idle/learn-mode or summer-cutoff
    stretch. The gain dimension must never be added from these."""
    rng = random.Random(seed)
    indoor = 21.0
    theta_env, theta_solar = 0.05, 0.40
    cycles = []
    for _k in range(n_cycles):
        outdoor = rng.uniform(-10.0, 10.0)
        solar = rng.uniform(0.0, 1.0)
        d = theta_env * (outdoor - indoor) * DT_H + theta_solar * solar * DT_H
        cycles.append(
            dict(
                indoor_temp_c=indoor,
                outdoor_temp_c=outdoor,
                compensation_delta_c=0.0,   # the crucial part: no excitation
                solar_effect=solar,
            )
        )
        indoor += d
    return cycles


def test_cold_start_reports_gain_not_modeled():
    state = initial_state()
    _new, result = step(state, make_inputs(), RCModelConfig())
    assert result.gain_modeled is False
    assert result.theta_gain == 0.0
    assert len(state.theta) == 2


def test_gain_not_added_without_excitation():
    # A long passive stretch (u == 0 throughout) must NOT add the gain
    # dimension, and must NOT wind up covariance: this is the regression test
    # for the summer/switch-off windup bug.
    cycles = _passive_cycles(2000, seed=9)
    state, result = _run(cycles)
    assert state.has_gain is False
    assert result.gain_modeled is False
    assert len(state.theta) == 2
    # No unexcited dimension -> the trace never runs toward its cap.
    assert result.cov_trace < 100.0
    assert result.cov_trace <= rc_model.P_TRACE_MAX


def test_add_gain_dimension_block_embeds_exactly():
    # Unit test of the pure embedding helper: old values/covariance preserved,
    # gain appended LAST with a fresh prior variance and zero cross-covariance.
    theta = [0.05, 0.40, 0.20]                    # e.g. env, solar, wind
    p = [
        [1.1, 0.2, 0.3],
        [0.2, 2.2, 0.4],
        [0.3, 0.4, 3.3],
    ]
    new_theta, new_p = rc_model._add_gain_dimension(theta, p)
    assert new_theta == [0.05, 0.40, 0.20, rc_model.INIT_THETA_GAIN]
    # Top-left 3x3 block identical.
    for i in range(3):
        for j in range(3):
            assert new_p[i][j] == p[i][j]
    # New gain diagonal is the wide prior; cross-covariance all zero.
    assert new_p[3][3] == rc_model._INIT_P["gain"]
    for i in range(3):
        assert new_p[i][3] == 0.0
        assert new_p[3][i] == 0.0


def test_gain_added_on_first_excitation_preserving_prior_learning():
    config = RCModelConfig()
    # Learn env/solar passively (no gain dimension). Kept below the warmup count
    # so the adaptive sigma gate stays off and the excitation cycles below are
    # accepted unconditionally — env/solar still move well off their priors
    # (true env 0.05 > prior 0.0333; true solar 0.40 > prior 0.0), which is all
    # this test needs to prove the expansion preserves rather than resets them.
    state, _ = _run(_passive_cycles(15, seed=3))
    assert state.has_gain is False and len(state.theta) == 2

    # One cycle carrying a real applied delta -> stored as prev_u_c (no expansion
    # yet: this step's own prev_u_c was still 0).
    state, res_a = step(
        state, make_inputs(indoor_temp_c=21.0, outdoor_temp_c=4.0, compensation_delta_c=-2.0), config
    )
    assert state.has_gain is False and len(state.theta) == 2
    env_before = state.theta[0]
    solar_before = state.theta[1]

    # Next accepted cycle sees prev_u_c = -2 -> expands, embedding the prior.
    state, res_b = step(
        state, make_inputs(indoor_temp_c=21.05, outdoor_temp_c=4.0, compensation_delta_c=-2.0), config
    )
    assert state.has_gain is True
    assert res_b.gain_modeled is True
    assert len(state.theta) == 3            # [env, solar, gain], gain appended LAST
    # The learned env/solar were NOT reset to their cold-start prior by the
    # expansion. After the embedding + a single RLS step they stay FAR closer to
    # their pre-expansion (learned) values than to the priors they would snap
    # back to on a reset — the decisive evidence of block-embedding preservation.
    assert abs(state.theta[0] - env_before) < abs(env_before - rc_model.INIT_THETA_ENV)
    assert abs(state.theta[1] - solar_before) < abs(solar_before - rc_model.INIT_THETA_SOLAR)
    # And solar had genuinely converged near its truth (0.40), so this is a real
    # learned value being preserved, not a still-at-prior coincidence.
    assert solar_before > 0.2
    assert "gain dimension added" in res_b.reason


def test_gain_stays_added_after_excitation_stops():
    # Once added, gain is permanent even if excitation later disappears.
    config = RCModelConfig()
    state, _ = _run(_passive_cycles(15, seed=1))  # immature -> sigma gate off
    # Excite a few times to add the dimension.
    for _ in range(3):
        state, _ = step(
            state,
            make_inputs(compensation_delta_c=-2.0, indoor_temp_c=21.0),
            config,
        )
    assert state.has_gain is True and len(state.theta) == 3
    # Now go passive again; the dimension must remain.
    for c in _passive_cycles(30, seed=2):
        state, result = step(state, make_inputs(**c), config)
    assert state.has_gain is True
    assert result.gain_modeled is True
    assert len(state.theta) == 3
