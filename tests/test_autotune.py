"""Unit tests for the pure auto-tuning derivation.

autotune.py has zero Home Assistant dependency, so it's loaded directly by file
path here (mirroring test_heuristic.py / test_rc_model.py) rather than via
`custom_components.climate_optimizer`, which would pull in `homeassistant`
through the package's __init__.py.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PATH = (
    Path(__file__).parent.parent
    / "custom_components"
    / "climate_optimizer"
    / "autotune.py"
)
_spec = importlib.util.spec_from_file_location("autotune", _PATH)
autotune = importlib.util.module_from_spec(_spec)
sys.modules["autotune"] = autotune
_spec.loader.exec_module(autotune)

AutotuneInputs = autotune.AutotuneInputs
derive = autotune.derive

# A plausible converged house: tau_open = 1/0.033 ~ 30 h, a clearly negative
# heat-pump gain (colder spoof -> more heat -> indoor rises), modest solar.
THETA_ENV = 1.0 / 30.0
THETA_GAIN = -0.10
THETA_SOLAR = 0.20


def make_inputs(**overrides) -> AutotuneInputs:
    defaults = dict(
        theta_env=THETA_ENV,
        theta_gain=THETA_GAIN,
        theta_solar=THETA_SOLAR,
        theta_wind=0.0,
        gain_modeled=True,
        params_pinned=False,
        accepted_samples=autotune.WARMUP_SAMPLES,  # fully matured by default
        indoor_temp_c=21.0,
        outdoor_temp_c=0.0,
        heating_type=autotune.HEATING_TYPE_RADIATORS,
        wind_reference_ms=5.0,
        max_heating_delta_c=8.0,
        tier_max_sag_c=1.5,
        enable_solar=True,
        enable_wind=False,
        manual_k_indoor=1.5,
        manual_k_wind=0.3,
        manual_k_sun=3.0,
        manual_k_price=5.0,
        manual_cold_taper_factor=1.0,
    )
    defaults.update(overrides)
    return AutotuneInputs(**defaults)


# --- tau_cl selection ---------------------------------------------------------


def test_tau_cl_is_open_loop_over_speedup():
    # 30 h house / speedup 3 = 10 h, comfortably above the radiator floor.
    result = derive(make_inputs())
    assert result.tau_open_h == 1.0 / THETA_ENV
    assert result.tau_cl_h == (1.0 / THETA_ENV) / autotune.CLOSED_LOOP_SPEEDUP


def test_underfloor_floor_binds_on_a_light_house():
    # A leaky 9 h house would want tau_cl = 3 h, but underfloor emitters can't
    # deliver that fast, so the 4 h floor must win. This is the entire reason
    # the heating-type question exists.
    light = make_inputs(theta_env=1.0 / 9.0, heating_type=autotune.HEATING_TYPE_UNDERFLOOR)
    assert derive(light).tau_cl_h == autotune.EMITTER_TAU_FLOOR_H["underfloor"]
    # The same house with radiators is allowed the faster response.
    rad = make_inputs(theta_env=1.0 / 9.0, heating_type=autotune.HEATING_TYPE_RADIATORS)
    assert derive(rad).tau_cl_h == 3.0


def test_tau_cl_capped_for_an_extremely_heavy_house():
    heavy = make_inputs(theta_env=1.0 / 400.0)  # tau_open/3 = 133 h
    assert derive(heavy).tau_cl_h == autotune.TAU_CL_MAX_H


def test_unknown_heating_type_still_gets_a_real_floor():
    # Degrades to the radiator floor, not to "no floor at all".
    assert autotune.emitter_tau_floor_h("gibberish") == autotune.EMITTER_TAU_FLOOR_H[
        autotune.HEATING_TYPE_RADIATORS
    ]
    assert autotune.emitter_tau_floor_h(None) > 0.0


# --- the derivation itself ----------------------------------------------------


def test_k_indoor_is_the_inverted_gain_law():
    result = derive(make_inputs())
    expected = 1.0 / (result.tau_cl_h * abs(THETA_GAIN))
    assert result.k_indoor_derived == expected


def test_k_sun_offsets_the_estimated_solar_gain():
    result = derive(make_inputs())
    assert result.k_sun_derived == THETA_SOLAR / abs(THETA_GAIN)


def test_k_price_is_more_aggressive_than_k_indoor():
    # Braking is bounded by the comfort floor and self-limiting; heating
    # overshoots with nothing to catch it. The asymmetry is deliberate.
    result = derive(make_inputs())
    assert result.k_price_derived > result.k_indoor_derived


def test_stronger_gain_yields_smaller_coefficients():
    """The whole point: a non-linear heat curve moves theta_gain with outdoor
    temperature, and the derived coefficients must move inversely so the loop
    gain stays constant across the season."""
    weak = derive(make_inputs(theta_gain=-0.05)).k_indoor_derived
    strong = derive(make_inputs(theta_gain=-0.20)).k_indoor_derived
    assert strong < weak
    # Specifically: 4x the plant gain, 1/4 the controller gain.
    assert abs(strong * 4.0 - weak) < 1e-9


def test_disabled_inputs_derive_to_zero_coefficients():
    result = derive(make_inputs(enable_solar=False, enable_wind=False))
    assert result.k_sun_derived == 0.0
    assert result.k_wind_derived == 0.0


def test_k_wind_scales_with_the_temperature_gap():
    """Wind loss physically scales with the indoor/outdoor gap, so the derived
    coefficient does too — fixing the dimensional inconsistency in the
    hand-tuned form, which has no gap factor."""
    mild = derive(make_inputs(enable_wind=True, theta_wind=0.1, outdoor_temp_c=15.0))
    cold = derive(make_inputs(enable_wind=True, theta_wind=0.1, outdoor_temp_c=-15.0))
    assert cold.k_wind_derived > mild.k_wind_derived


def test_wind_stands_down_without_an_indoor_reading():
    # No indoor temperature means no gap to scale by; guessing one would be
    # worse than contributing nothing.
    result = derive(
        make_inputs(enable_wind=True, theta_wind=0.1, indoor_temp_c=None)
    )
    assert result.k_wind_derived == 0.0


# --- trust gates --------------------------------------------------------------


def test_gain_not_modeled_falls_back_to_manual():
    result = derive(make_inputs(gain_modeled=False, theta_gain=0.0))
    assert result.usable is False
    assert result.blend_weight == 0.0
    assert result.k_indoor_effective == 1.5
    assert result.k_price_effective == 5.0
    assert "not yet excited" in result.reason


def test_tiny_gain_falls_back_to_manual():
    # 1/theta_gain would explode; more importantly we can't tell how the pump
    # responds at all.
    result = derive(make_inputs(theta_gain=-1e-5))
    assert result.usable is False
    assert result.k_indoor_effective == 1.5


def test_pinned_params_fall_back_to_manual():
    result = derive(make_inputs(params_pinned=True))
    assert result.usable is False
    assert "pinned" in result.reason


def test_non_physical_theta_env_falls_back_to_manual():
    result = derive(make_inputs(theta_env=0.0))
    assert result.usable is False


def test_derived_values_are_clamped_to_the_manual_ranges():
    # A pathologically small gain that still clears MIN_GAIN_MAGNITUDE would
    # derive k_indoor = 1/(10 * 0.011) ~ 9.1 — inside the range; push further
    # with a fast tau_cl to force the clamp and assert it is reported.
    result = derive(
        make_inputs(theta_gain=-0.011, theta_env=1.0 / 6.0)  # tau_cl = 2 h (floor)
    )
    assert result.k_indoor_derived <= autotune.K_INDOOR_MAX
    assert result.clamped is True
    assert "clamp" in result.reason


# --- the evidence ramp --------------------------------------------------------


def test_zero_evidence_is_exactly_manual():
    """Day one of Auto mode must behave identically to Manual mode — the ramp is
    what makes defaulting to Auto safe."""
    result = derive(make_inputs(accepted_samples=0))
    assert result.usable is True  # the model is trusted...
    assert result.blend_weight == 0.0  # ...but has no evidence yet
    assert result.k_indoor_effective == 1.5
    assert result.k_wind_effective == 0.3
    assert result.k_sun_effective == 3.0
    assert result.k_price_effective == 5.0


def test_full_evidence_is_exactly_derived():
    result = derive(make_inputs(accepted_samples=autotune.WARMUP_SAMPLES))
    assert result.blend_weight == 1.0
    assert result.k_indoor_effective == result.k_indoor_derived
    assert result.k_price_effective == result.k_price_derived


def test_half_evidence_lands_halfway():
    result = derive(make_inputs(accepted_samples=autotune.WARMUP_SAMPLES // 2))
    assert abs(result.blend_weight - 0.5) < 1e-9
    midpoint = 0.5 * (1.5 + result.k_indoor_derived)
    assert abs(result.k_indoor_effective - midpoint) < 1e-9


def test_ramp_is_monotonic_and_saturates():
    weights = [
        derive(make_inputs(accepted_samples=n)).blend_weight
        for n in (0, 100, 240, 480, 5000)
    ]
    assert weights == sorted(weights)
    assert weights[-1] == 1.0


# --- recovery-feasibility taper (the cold-taper replacement) ------------------


def test_taper_shrinks_as_it_gets_colder():
    """The taper's shape must fall out of the physics rather than being drawn by
    hand: the envelope loss term eats the recovery rate in deep cold."""
    mild = autotune.derived_cold_taper(
        THETA_ENV, THETA_GAIN, 10.0, outdoor_c=5.0, indoor_c=21.0,
        max_heating_delta_c=8.0, tier_max_sag_c=1.5,
    )
    cold = autotune.derived_cold_taper(
        THETA_ENV, THETA_GAIN, 10.0, outdoor_c=-25.0, indoor_c=21.0,
        max_heating_delta_c=8.0, tier_max_sag_c=1.5,
    )
    assert cold < mild


def test_taper_is_zero_when_recovery_is_impossible():
    # Heating authority too weak to overcome the envelope loss: sagging would
    # spend comfort with no way to buy it back.
    factor = autotune.derived_cold_taper(
        theta_env=0.05, theta_gain=-0.001, tau_cl_h=10.0,
        outdoor_c=-30.0, indoor_c=21.0,
        max_heating_delta_c=1.0, tier_max_sag_c=1.5,
    )
    assert factor == 0.0


def test_taper_is_one_when_the_sag_is_easily_recoverable():
    factor = autotune.derived_cold_taper(
        THETA_ENV, THETA_GAIN, 10.0, outdoor_c=10.0, indoor_c=21.0,
        max_heating_delta_c=8.0, tier_max_sag_c=0.5,
    )
    assert factor == 1.0


def test_taper_irrelevant_when_the_tier_does_not_sag():
    factor = autotune.derived_cold_taper(
        THETA_ENV, THETA_GAIN, 10.0, outdoor_c=-20.0, indoor_c=21.0,
        max_heating_delta_c=8.0, tier_max_sag_c=0.0,
    )
    assert factor == 1.0


def test_taper_falls_back_to_manual_without_an_indoor_reading():
    result = derive(make_inputs(indoor_temp_c=None, manual_cold_taper_factor=0.42))
    assert result.cold_taper_derived == 0.42


# --- tuning-mode resolution ---------------------------------------------------


def test_unknown_tuning_mode_degrades_to_manual():
    # Never to Auto: an unrecognized value must not silently hand control to
    # the derived coefficients.
    assert autotune.resolve_tuning_mode("nonsense") == autotune.TUNING_MODE_MANUAL
    assert autotune.resolve_tuning_mode(None) == autotune.TUNING_MODE_MANUAL
    assert autotune.resolve_tuning_mode("  AUTO ") == autotune.TUNING_MODE_AUTO


# --- JSONL log fields ---------------------------------------------------------

MANUAL = (1.5, 0.3, 3.0, 5.0)


def test_log_fields_carry_manual_derived_and_effective():
    """All three sets must be logged: only `effective` is unrecoverable from
    anything else, and the other two make offline comparison possible without
    re-deriving."""
    result = derive(make_inputs())
    fields = autotune.log_fields(result, "auto", MANUAL)
    for name in ("k_indoor", "k_wind", "k_sun", "k_price"):
        assert f"{name}_manual" in fields
        assert f"{name}_derived" in fields
        assert f"{name}_effective" in fields
    assert fields["tuning_mode"] == "auto"
    assert fields["k_indoor_manual"] == 1.5
    assert fields["k_indoor_effective"] == result.k_indoor_effective


def test_log_fields_are_json_serializable():
    import json

    fields = autotune.log_fields(derive(make_inputs()), "auto", MANUAL)
    assert json.loads(json.dumps(fields)) == fields


def test_log_fields_without_a_result_still_records_manual_and_why():
    fields = autotune.log_fields(None, "manual", MANUAL)
    assert fields["autotune_usable"] is False
    assert "no RC model result" in fields["autotune_block_reason"]
    # The manual values are still logged, so a replay knows what was in force.
    assert fields["k_indoor_manual"] == 1.5


def test_block_reason_logged_only_when_unusable():
    # Rare + diagnostic: worth the bytes. The full reason string every cycle
    # would dominate the file for something reconstructible from the numbers.
    blocked = autotune.log_fields(
        derive(make_inputs(gain_modeled=False)), "auto", MANUAL
    )
    assert "autotune_block_reason" in blocked

    healthy = autotune.log_fields(derive(make_inputs()), "auto", MANUAL)
    assert "autotune_block_reason" not in healthy


def test_log_fields_record_the_ramp_and_clamp_flags():
    fields = autotune.log_fields(
        derive(make_inputs(accepted_samples=autotune.WARMUP_SAMPLES // 4)),
        "auto",
        MANUAL,
    )
    assert abs(fields["autotune_blend_weight"] - 0.25) < 1e-9
    assert fields["autotune_clamped"] is False
    assert fields["autotune_tau_cl_h"] > 0.0
