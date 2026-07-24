"""Unit tests for the pure MPC (dynamic-programming) planner.

mpc.py has zero Home Assistant dependency, so it's loaded directly by file
path here (mirroring test_heuristic.py / test_rc_model.py) rather than via
`custom_components.climate_optimizer`, which would pull in `homeassistant`
through the package's __init__.py.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

_MPC_PATH = (
    Path(__file__).parent.parent
    / "custom_components"
    / "climate_optimizer"
    / "mpc.py"
)
_spec = importlib.util.spec_from_file_location("mpc", _MPC_PATH)
mpc = importlib.util.module_from_spec(_spec)
sys.modules["mpc"] = mpc
_spec.loader.exec_module(mpc)

MPCModelParams = mpc.MPCModelParams
MPCConfig = mpc.MPCConfig
MPCForecasts = mpc.MPCForecasts
plan = mpc.plan


# --- fixtures / helpers -------------------------------------------------------

# A physically plausible, mature RC estimate: tau=20h, gain clearly negative,
# modest solar. Trust gate passes with these unless a test overrides.
def make_params(**overrides) -> MPCModelParams:
    defaults = dict(
        theta_env=0.02,        # tau = 50 h (reasonably insulated house)
        theta_gain=-0.20,      # colder reading -> more heat (physical sign)
        theta_solar=0.10,
        theta_wind=0.0,
        enable_wind=False,
        wind_reference_ms=5.0,
        confidence=1.0,
        accepted_samples=500,
        params_pinned=False,
    )
    defaults.update(overrides)
    return MPCModelParams(**defaults)


def make_config(**overrides) -> MPCConfig:
    defaults = dict(
        horizon_hours=24.0,
        step_hours=1.0,
        temp_grid_step_c=0.25,
        control_steps=9,
        max_heating_delta_c=8.0,
        comfort_min_c=19.0,
        comfort_max_c=22.0,
        indoor_target_c=21.0,
        min_confidence=1.0,
        min_accepted_samples=20,
        min_gain_magnitude=0.01,
    )
    defaults.update(overrides)
    return MPCConfig(**defaults)


def make_forecasts(
    steps=24,
    price=None,
    outdoor=0.0,
    solar=0.0,
    wind=0.0,
    valid_steps=None,
) -> MPCForecasts:
    if price is None:
        price = [1.0] * steps
    elif isinstance(price, (int, float)):
        price = [float(price)] * steps
    outdoor = [outdoor] * steps if isinstance(outdoor, (int, float)) else list(outdoor)
    solar = [solar] * steps if isinstance(solar, (int, float)) else list(solar)
    wind = [wind] * steps if isinstance(wind, (int, float)) else list(wind)
    return MPCForecasts(
        price=tuple(price),
        outdoor_temp_c=tuple(outdoor),
        solar_effect=tuple(solar),
        wind_speed_ms=tuple(wind),
        valid_steps=steps if valid_steps is None else valid_steps,
    )


# --- basic structure / degenerate inputs -------------------------------------


def test_no_indoor_data_returns_no_data_status():
    result = plan(None, make_params(), make_forecasts(), make_config())
    assert result.status == "no_data"
    assert result.recommended_delta_c is None
    assert result.trustworthy is False


def test_no_forecast_returns_no_forecast_status():
    empty = MPCForecasts(price=(), outdoor_temp_c=(), solar_effect=(), wind_speed_ms=(), valid_steps=0)
    result = plan(21.0, make_params(), empty, make_config())
    assert result.status == "no_forecast"
    assert result.recommended_delta_c is None


def test_mismatched_forecast_lengths_rejected():
    bad = MPCForecasts(
        price=(1.0, 1.0),
        outdoor_temp_c=(0.0,),  # wrong length
        solar_effect=(0.0, 0.0),
        wind_speed_ms=(0.0, 0.0),
        valid_steps=2,
    )
    result = plan(21.0, make_params(), bad, make_config())
    assert result.status == "no_forecast"


def test_degenerate_comfort_band_reported():
    result = plan(
        21.0, make_params(), make_forecasts(), make_config(comfort_min_c=22.0, comfort_max_c=22.0)
    )
    assert result.status == "degenerate"


def test_trajectory_length_matches_steps_plus_one():
    steps = 12
    result = plan(21.0, make_params(), make_forecasts(steps=steps), make_config(horizon_hours=12))
    assert result.steps_planned == steps
    assert len(result.predicted_trajectory) == steps + 1
    assert len(result.plan) == steps
    # Trajectory starts at the actual current temperature.
    assert result.predicted_trajectory[0] == 21.0


# --- comfort-bound guarantee (the safety-critical property) ------------------


def test_comfort_bounds_never_violated_under_flat_prices():
    # Cold outside, house starts mid-band; the plan must keep every predicted
    # step within [comfort_min, comfort_max].
    result = plan(
        20.5,
        make_params(),
        make_forecasts(steps=24, price=1.0, outdoor=-10.0),
        make_config(),
    )
    assert result.status == "ok"
    for t in result.predicted_trajectory:
        assert result.plan and 19.0 - 1e-6 <= t <= 22.0 + 1e-6, t
    assert result.binding_constraint is not None


def test_comfort_bounds_never_violated_under_adversarial_price_spike():
    # Adversarial: a huge price spike in the middle. A cost-blind optimizer
    # might try to avoid all heating during the spike and let the house crash
    # through the floor. The comfort penalty must prevent any violation.
    steps = 24
    price = [1.0] * steps
    for i in range(8, 16):
        price[i] = 1000.0  # extreme spike
    result = plan(
        21.5,
        make_params(),
        make_forecasts(steps=steps, price=price, outdoor=-15.0),
        make_config(),
    )
    assert result.status == "ok"
    for step in result.plan:
        assert step.comfort_violation_c <= 1e-6
    for t in result.predicted_trajectory:
        assert 19.0 - 1e-6 <= t <= 22.0 + 1e-6, t


def test_starting_below_band_is_flagged_infeasible_not_crashing():
    # House already well below the comfort floor and outside very cold: even
    # full heat may not recover in one step. Must return a best-effort plan and
    # flag infeasible rather than raising or returning None.
    result = plan(
        10.0,  # far below comfort_min 19
        make_params(theta_gain=-0.02),  # weak authority -> can't jump back fast
        make_forecasts(steps=24, price=1.0, outdoor=-20.0),
        make_config(),
    )
    assert result.recommended_delta_c is not None
    assert result.status == "infeasible"
    assert "infeasible" in result.binding_constraint.lower()
    # Best effort should still call for maximum heating (most negative u).
    assert result.recommended_delta_c == min(mpc._controls(make_config()))


# --- price-driven load shifting (the value proposition) ----------------------


def test_preheats_before_a_known_price_spike():
    # Cheap now, very expensive soon. With thermal storage available (a wide
    # comfort band) the optimizer should heat harder now (more negative delta)
    # than a myopic hold-target baseline would, banking heat before the spike.
    steps = 12
    price = [1.0] * steps
    for i in range(4, 12):
        price[i] = 50.0
    cfg = make_config(horizon_hours=12, comfort_min_c=19.0, comfort_max_c=23.0)
    result = plan(21.0, make_params(), make_forecasts(steps=steps, price=price, outdoor=-8.0), cfg)

    assert result.status == "ok"
    # It should bank heat: the very first step heats (delta clearly negative)
    # and the predicted trajectory rises above the starting point before the
    # spike, using the comfort band as storage.
    assert result.recommended_delta_c < 0.0
    pre_spike_peak = max(result.predicted_trajectory[:5])
    assert pre_spike_peak > 21.0
    # And it should beat the myopic baseline on cost.
    assert result.projected_savings >= 0.0


def test_cheaper_to_shift_yields_positive_savings():
    # A clear cheap/expensive split with storage room should let MPC save vs a
    # myopic hold-target baseline.
    steps = 24
    price = [1.0 if i < 12 else 20.0 for i in range(steps)]
    cfg = make_config(comfort_min_c=19.0, comfort_max_c=23.0)
    result = plan(21.0, make_params(), make_forecasts(steps=steps, price=price, outdoor=-6.0), cfg)
    assert result.status == "ok"
    assert result.projected_savings > 0.0
    assert result.total_cost <= result.baseline_cost + 1e-9


def test_mpc_never_costs_more_than_the_myopic_baseline():
    # Fundamental optimality property: the DP optimum can never be beaten by the
    # myopic hold-target baseline it is compared against, under any price shape.
    for price in (
        [3.0] * 24,                                   # flat
        [1.0 if i < 12 else 20.0 for i in range(24)], # cheap then dear
        [20.0 if i < 12 else 1.0 for i in range(24)], # dear then cheap
        [1.0 + (i % 6) for i in range(24)],           # sawtooth
    ):
        result = plan(
            21.0, make_params(), make_forecasts(steps=24, price=price, outdoor=-5.0), make_config()
        )
        assert result.status == "ok"
        assert result.total_cost <= result.baseline_cost + 1e-6
        assert result.projected_savings >= -1e-6


def test_flat_price_runs_house_toward_the_comfort_floor():
    # With a flat price the only lever is total energy: the optimum sits the
    # house near the comfort floor (least envelope loss), not at the warmer
    # target the baseline holds. This is legitimate saving, not load-shifting.
    result = plan(
        21.0, make_params(), make_forecasts(steps=24, price=3.0, outdoor=-5.0),
        make_config(comfort_min_c=19.0, comfort_max_c=22.0, indoor_target_c=21.0),
    )
    assert result.status == "ok"
    # Interior of the horizon should trend below the 21 target toward the floor.
    interior = result.predicted_trajectory[3:-3]
    assert min(interior) < 20.5


# --- heating-only invariant ---------------------------------------------------


def test_control_is_heating_only_never_positive():
    # Every control level offered (and thus every recommendation) must be <= 0:
    # the planner adds heat or coasts, it never commands cooling.
    controls = mpc._controls(make_config())
    assert max(controls) == 0.0
    assert min(controls) == -8.0
    steps = 24
    price = [1.0] * steps
    result = plan(21.5, make_params(), make_forecasts(steps=steps, price=price, outdoor=5.0), make_config())
    for step in result.plan:
        assert step.control_delta_c <= 1e-9


def test_warm_outdoor_lets_house_coast_without_heating():
    # Outdoor within the comfort band and no price pressure: the house holds
    # itself, so the optimizer should mostly coast (near-zero heat) rather than
    # burn energy.
    result = plan(
        21.0,
        make_params(),
        make_forecasts(steps=24, price=1.0, outdoor=20.0),
        make_config(comfort_min_c=19.0, comfort_max_c=22.0),
    )
    assert result.status == "ok"
    # Total heat delivered across the horizon should be small.
    total_heat = sum(max(0.0, s.heat_delivered_c) for s in result.plan)
    assert total_heat < 1.0


# --- trustworthiness gating ---------------------------------------------------


def test_immature_model_is_not_trustworthy_but_still_plans():
    result = plan(
        21.0,
        make_params(confidence=0.3, accepted_samples=6),
        make_forecasts(),
        make_config(min_confidence=1.0, min_accepted_samples=20),
    )
    assert result.trustworthy is False
    assert result.status == "not_trustworthy"
    # Still produces an observable plan.
    assert result.recommended_delta_c is not None
    assert "warming up" in result.reason or "confidence" in result.reason


def test_gain_not_yet_modeled_is_reported_distinctly():
    # Heat pump never excited: gain dimension not added yet. This must be
    # flagged not_trustworthy with an "excited"/"applied" reason distinct from
    # a fitted-but-implausible gain, and NOT be conflated with the gain-sign
    # or magnitude failures. theta_gain here is the not-yet-modeled 0.0.
    # Warm outdoor so a coasting (zero-heat) house stays inside the band and the
    # plan is feasible -> the status is exactly not_trustworthy, isolating the
    # gate reason under test from any infeasibility.
    result = plan(
        21.0,
        make_params(theta_gain=0.0, gain_modeled=False),
        make_forecasts(outdoor=21.0),
        make_config(),
    )
    assert result.trustworthy is False
    assert result.status == "not_trustworthy"
    assert result.gain_modeled is False
    reason = result.reason.lower()
    assert "excited" in reason or "applied" in reason
    # Distinct from the "not clearly negative / cannot tell how the pump
    # responds" wording used for a fitted-but-implausible gain.
    assert "cannot tell how the pump responds" not in reason
    # Still produces an observable plan (advisory groundwork).
    assert result.recommended_delta_c is not None


def test_gain_modeled_true_by_default_still_trustworthy():
    # Regression: existing callers/params default gain_modeled=True, so a mature
    # plausible model stays trustworthy exactly as before.
    result = plan(21.0, make_params(), make_forecasts(), make_config())
    assert result.gain_modeled is True
    assert result.trustworthy is True


def test_wrong_sign_gain_is_not_trustworthy():
    # Positive gain is non-physical for this model (colder reading should add
    # heat). Must be flagged untrustworthy.
    result = plan(
        21.0,
        make_params(theta_gain=+0.2),
        make_forecasts(),
        make_config(),
    )
    assert result.trustworthy is False
    assert result.status in ("not_trustworthy", "infeasible")
    assert "gain" in result.reason.lower()


def test_pinned_parameters_flagged_untrustworthy():
    result = plan(
        21.0,
        make_params(params_pinned=True),
        make_forecasts(),
        make_config(),
    )
    assert result.trustworthy is False
    assert "pinned" in result.reason.lower()


def test_near_zero_gain_flagged_untrustworthy():
    result = plan(
        21.0,
        make_params(theta_gain=-0.001),  # below min_gain_magnitude
        make_forecasts(),
        make_config(min_gain_magnitude=0.01),
    )
    assert result.trustworthy is False
    assert "gain" in result.reason.lower()


def test_mature_plausible_model_is_trustworthy():
    result = plan(21.0, make_params(), make_forecasts(price=1.0, outdoor=-5.0), make_config())
    assert result.trustworthy is True
    assert result.status == "ok"


# --- forecast truncation reporting -------------------------------------------


def test_partial_forecast_is_reported_in_reason():
    result = plan(
        21.0,
        make_params(),
        make_forecasts(steps=24, price=1.0, outdoor=-5.0, valid_steps=6),
        make_config(),
    )
    assert result.forecast_valid_steps == 6
    assert "extrapolated" in result.reason


# --- RC parameter echoes (troubleshooting requirement) -----------------------


def test_result_echoes_rc_parameters_used():
    params = make_params(theta_env=0.04, theta_gain=-0.2, theta_solar=0.3)
    result = plan(21.0, params, make_forecasts(outdoor=-5.0), make_config())
    assert result.theta_env == 0.04
    assert result.theta_gain == -0.2
    assert result.theta_solar == 0.3
    assert math.isclose(result.time_constant_h, 25.0, rel_tol=1e-9)
    assert result.confidence == 1.0
    assert result.accepted_samples == 500
    # The reason string surfaces them for at-a-glance troubleshooting.
    assert "gain=" in result.reason
    assert "tau=" in result.reason


def test_wind_params_echoed_when_enabled():
    params = make_params(enable_wind=True, theta_wind=0.12, wind_reference_ms=5.0)
    result = plan(
        21.0, params, make_forecasts(outdoor=-5.0, wind=10.0), make_config()
    )
    assert result.theta_wind == 0.12
    assert "wind=" in result.reason


# --- wind term affects dynamics when enabled ---------------------------------


def test_wind_increases_heat_demand_when_enabled():
    # With a leaky (wind-enabled) house, a cold + windy horizon needs more heat
    # to hold comfort than a calm one -> higher total cost. theta_wind is small
    # because the interaction term already carries the large (T_out - T) gap.
    steps = 24
    fc_calm = make_forecasts(steps=steps, price=1.0, outdoor=-5.0, wind=0.0)
    fc_windy = make_forecasts(steps=steps, price=1.0, outdoor=-5.0, wind=8.0)
    params = make_params(enable_wind=True, theta_wind=0.02, wind_reference_ms=5.0)
    cfg = make_config()
    calm = plan(21.0, params, fc_calm, cfg)
    windy = plan(21.0, params, fc_windy, cfg)
    assert calm.status == "ok" and windy.status == "ok"
    assert windy.total_cost > calm.total_cost


# --- determinism / receding-horizon sanity -----------------------------------


def test_plan_is_deterministic():
    args = (21.0, make_params(), make_forecasts(price=[1.0 + (i % 5) for i in range(24)], outdoor=-5.0), make_config())
    a = plan(*args)
    b = plan(*args)
    assert a.recommended_delta_c == b.recommended_delta_c
    assert a.total_cost == b.total_cost
    assert a.predicted_trajectory == b.predicted_trajectory


def test_receding_horizon_reoptimizes_from_new_state():
    # Re-solving from a colder start should call for at least as much heat as
    # from a warmer start under identical forecasts (monotone in the obvious
    # direction), confirming the plan tracks the current state.
    fc = make_forecasts(steps=24, price=1.0, outdoor=-8.0)
    cfg = make_config()
    warm = plan(21.5, make_params(), fc, cfg)
    cold = plan(19.5, make_params(), fc, cfg)
    assert cold.recommended_delta_c <= warm.recommended_delta_c + 1e-9


def test_terminal_target_pulls_final_temperature_up():
    # The reheat-priced terminal cost should make the plan end warmer when the
    # target (reference) is high than when it is at the floor, under an
    # otherwise identical flat-price horizon. This verifies the terminal cost
    # governs the horizon boundary rather than the optimizer draining heat to
    # the floor purely because the horizon ends.
    fc = make_forecasts(steps=24, price=1.0, outdoor=-6.0)
    high = plan(
        21.0, make_params(), fc,
        make_config(comfort_min_c=19.0, comfort_max_c=22.0, indoor_target_c=22.0),
    )
    low = plan(
        21.0, make_params(), fc,
        make_config(comfort_min_c=19.0, comfort_max_c=22.0, indoor_target_c=19.0),
    )
    assert high.status == "ok" and low.status == "ok"
    assert high.predicted_trajectory[-1] > low.predicted_trajectory[-1]


# --- control granularity edge cases ------------------------------------------


def test_zero_heating_authority_still_returns_a_plan():
    # max_heating_delta_c = 0 -> only control is coast. Must not crash; house
    # simply floats and the plan is (best-effort) infeasible if it leaves band.
    result = plan(
        21.0,
        make_params(),
        make_forecasts(steps=24, price=1.0, outdoor=-10.0),
        make_config(max_heating_delta_c=0.0),
    )
    assert result.recommended_delta_c == 0.0
    # Cold outside + no heat -> will breach the floor -> infeasible flagged.
    assert result.status == "infeasible"
