"""Unit tests for the pure heuristic compute core.

heuristic.py has zero Home Assistant dependency, so it's loaded directly by
file path here rather than via `custom_components.climate_optimizer`, which
would otherwise pull in `homeassistant` through the package's __init__.py.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_HEURISTIC_PATH = (
    Path(__file__).parent.parent
    / "custom_components"
    / "climate_optimizer"
    / "heuristic.py"
)
_spec = importlib.util.spec_from_file_location("heuristic", _HEURISTIC_PATH)
heuristic = importlib.util.module_from_spec(_spec)
sys.modules["heuristic"] = heuristic
_spec.loader.exec_module(heuristic)

HeuristicInputs = heuristic.HeuristicInputs
HeuristicParams = heuristic.HeuristicParams
compute = heuristic.compute


def make_params(**overrides) -> HeuristicParams:
    defaults = dict(
        indoor_target_c=21.0,
        enable_price_compensation=False,
        k_indoor=1.5,
        k_wind=0.3,
        k_sun=3.0,
        comfort_min_c=18.0,
        comfort_max_c=23.0,
        price_threshold_start=1.5,
        price_threshold_max=3.0,
        price_max_drop_c=1.0,
        # High enough that no existing test's raw_outdoor_temp_c (default
        # 3.0) accidentally trips the heating cutoff; cutoff-specific tests
        # override this explicitly.
        heating_cutoff_c=100.0,
    )
    defaults.update(overrides)
    return HeuristicParams(**defaults)


def make_inputs(**overrides) -> HeuristicInputs:
    defaults = dict(
        indoor_temp_c=21.0,
        indoor_data_available=True,
        raw_outdoor_temp_c=3.0,
        wind_speed_ms=0.0,
        wind_data_available=True,
        sun_elevation_deg=0.0,
        cloud_coverage_pct=0.0,
        cloud_data_available=True,
        current_price=None,
        price_data_available=False,
    )
    defaults.update(overrides)
    return HeuristicInputs(**defaults)


def test_at_target_no_wind_no_sun_no_price_is_a_passthrough():
    result = compute(make_inputs(), make_params())
    assert result.compensated_outdoor_temp_c == result.raw_outdoor_temp_c
    assert result.indoor_adjustment_c == 0
    assert result.wind_adjustment_c == 0
    assert result.sun_adjustment_c == 0
    assert result.price_adjustment_c == 0


def test_colder_indoor_than_target_lowers_compensated_temp():
    result = compute(make_inputs(indoor_temp_c=19.0), make_params())
    # 2 degC below target * k_indoor 1.5 = -3 degC adjustment
    assert result.indoor_adjustment_c == -3.0
    assert result.compensated_outdoor_temp_c == 0.0


def test_wind_lowers_compensated_temp():
    result = compute(make_inputs(wind_speed_ms=10.0), make_params())
    assert result.wind_adjustment_c == -3.0
    assert result.compensated_outdoor_temp_c == 0.0


def test_sun_raises_compensated_temp():
    result = compute(
        make_inputs(sun_elevation_deg=90.0, cloud_coverage_pct=0.0), make_params()
    )
    assert result.solar_effect == 1.0
    assert result.sun_adjustment_c == 3.0
    assert result.compensated_outdoor_temp_c == 6.0


def test_full_cloud_cover_cancels_solar_effect():
    result = compute(
        make_inputs(sun_elevation_deg=90.0, cloud_coverage_pct=100.0), make_params()
    )
    assert result.solar_effect == 0.0
    assert result.sun_adjustment_c == 0.0


def test_sun_below_horizon_has_no_effect():
    result = compute(make_inputs(sun_elevation_deg=-10.0), make_params())
    assert result.solar_effect == 0.0
    assert result.sun_adjustment_c == 0.0


def test_wind_forecast_unavailable_is_flagged_and_noted_in_reason():
    result = compute(
        make_inputs(wind_speed_ms=0.0, wind_data_available=False), make_params()
    )
    assert result.wind_data_available is False
    assert "wind forecast unavailable" in result.reason


def test_cloud_forecast_unavailable_is_flagged_and_noted_in_reason():
    result = compute(
        make_inputs(cloud_coverage_pct=None, cloud_data_available=False),
        make_params(),
    )
    assert result.cloud_data_available is False
    assert "cloud/sun forecast unavailable" in result.reason


def test_wind_and_cloud_available_are_independent_flags():
    # Wind missing shouldn't affect the cloud flag or vice versa.
    result = compute(
        make_inputs(wind_data_available=False, cloud_data_available=True),
        make_params(),
    )
    assert result.wind_data_available is False
    assert result.cloud_data_available is True
    assert "wind forecast unavailable" in result.reason
    assert "cloud/sun forecast unavailable" not in result.reason


def test_price_disabled_ignores_price_even_if_data_available():
    result = compute(
        make_inputs(current_price=5.0, price_data_available=True),
        make_params(enable_price_compensation=False),
    )
    assert result.current_price is None
    assert result.price_shift_applied_c == 0.0
    assert result.price_adjustment_c == 0.0


def test_price_below_threshold_has_no_effect():
    result = compute(
        make_inputs(current_price=1.0, price_data_available=True),
        make_params(enable_price_compensation=True),
    )
    assert result.price_shift_applied_c == 0.0


def test_price_between_thresholds_is_convex_not_linear():
    # Mid tier gamma=2.0: at the ramp midpoint (price 2.25) the response is
    # 0.5**2 = 0.25, NOT 0.5 — small excursions are deliberately damped so the
    # controller only reacts hard to genuinely large price differences. Shift =
    # 0.25 * max_sag(1.5) * taper(1.0 at +3°C outdoor) = 0.375.
    result = compute(
        make_inputs(current_price=2.25, price_data_available=True),
        make_params(enable_price_compensation=True),
    )
    assert result.price_response == pytest.approx(0.25)
    assert result.price_shift_applied_c == pytest.approx(0.375)
    assert result.effective_indoor_target_c == pytest.approx(20.625)


def test_price_above_max_threshold_caps_at_tier_max_sag():
    # Mid tier: response saturates at 1.0, so shift = max_sag(1.5) * taper(1.0).
    # The compensated bump uses k_price (5.0), decoupled from k_indoor, so a
    # spike brakes far harder than the old +1.5°C: -5.0 * (19.5 - 21) = +7.5°C.
    result = compute(
        make_inputs(current_price=100.0, price_data_available=True),
        make_params(enable_price_compensation=True),
    )
    assert result.price_shift_applied_c == pytest.approx(1.5)
    assert result.effective_indoor_target_c == pytest.approx(19.5)
    assert result.price_adjustment_c == pytest.approx(7.5)


def test_price_shift_never_exceeds_comfort_min():
    # High tier sag 3.0 would push the target to 18.0; a comfort_min of 19.0
    # clamps it there regardless.
    result = compute(
        make_inputs(current_price=100.0, price_data_available=True),
        make_params(
            enable_price_compensation=True,
            price_comfort_tier="high",
            comfort_min_c=19.0,
        ),
    )
    assert result.effective_indoor_target_c == 19.0


def test_price_missing_data_soft_degrades_to_no_effect():
    result = compute(
        make_inputs(current_price=None, price_data_available=False),
        make_params(enable_price_compensation=True),
    )
    assert result.current_price is None
    assert result.price_shift_applied_c == 0.0


def test_output_is_clamped_to_sanity_band():
    result = compute(
        make_inputs(
            raw_outdoor_temp_c=20.0,
            indoor_temp_c=-50.0,  # absurd input, forces indoor_adjustment_c very negative
        ),
        make_params(k_indoor=1000.0),
    )
    assert result.compensated_outdoor_temp_c == heuristic.OUTPUT_SANITY_MIN_C


def test_reason_string_mentions_all_active_terms():
    result = compute(
        make_inputs(
            indoor_temp_c=19.0,
            wind_speed_ms=5.0,
            sun_elevation_deg=45.0,
            current_price=5.0,
            price_data_available=True,
        ),
        make_params(enable_price_compensation=True),
    )
    assert "Indoor" in result.reason
    assert "wind" in result.reason
    assert "sun" in result.reason
    assert "price" in result.reason


def test_indoor_sensor_unavailable_falls_back_to_raw_outdoor_temp():
    result = compute(
        make_inputs(indoor_temp_c=None, indoor_data_available=False),
        make_params(),
    )
    assert result.indoor_adjustment_c == 0.0
    assert result.indoor_temp_c is None
    assert result.compensated_outdoor_temp_c == result.raw_outdoor_temp_c
    assert "unavailable" in result.reason


def test_indoor_sensor_unavailable_does_not_suppress_other_terms():
    result = compute(
        make_inputs(
            indoor_temp_c=None,
            indoor_data_available=False,
            wind_speed_ms=10.0,
            sun_elevation_deg=90.0,
            cloud_coverage_pct=0.0,
        ),
        make_params(),
    )
    assert result.wind_adjustment_c == -3.0
    assert result.sun_adjustment_c == 3.0
    assert result.compensated_outdoor_temp_c == result.raw_outdoor_temp_c + (-3.0) + 3.0


def test_degenerate_price_thresholds_do_not_crash():
    # threshold_max <= threshold_start is a misconfiguration; must not raise.
    result = compute(
        make_inputs(current_price=5.0, price_data_available=True),
        make_params(
            enable_price_compensation=True,
            price_threshold_start=2.0,
            price_threshold_max=2.0,
        ),
    )
    # Hard step to full response → shift = mid-tier max_sag(1.5) * taper(1.0).
    assert result.price_shift_applied_c == pytest.approx(1.5)


def test_higher_tier_brakes_harder():
    # Same full spike, three tiers: sag scales 0.5 / 1.5 / 3.0, and the
    # compensated bump (k_price 5.0 × sag) scales with it.
    shifts = {}
    for tier in ("low", "mid", "high"):
        result = compute(
            make_inputs(current_price=100.0, price_data_available=True),
            make_params(enable_price_compensation=True, price_comfort_tier=tier),
        )
        shifts[tier] = result.price_shift_applied_c
    assert shifts["low"] == pytest.approx(0.5)
    assert shifts["mid"] == pytest.approx(1.5)
    assert shifts["high"] == pytest.approx(3.0)
    assert shifts["low"] < shifts["mid"] < shifts["high"]


def test_convex_curve_reacts_more_to_larger_price_differences():
    # A price a quarter of the way up the band should produce far less than a
    # quarter of the full response (convexity). Mid gamma=2.0: 0.25**2 = 0.0625.
    span = 3.0 - 1.5
    quarter_price = 1.5 + 0.25 * span
    result = compute(
        make_inputs(current_price=quarter_price, price_data_available=True),
        make_params(enable_price_compensation=True),
    )
    assert result.price_response == pytest.approx(0.0625)
    # Well under a linear quarter-response (which would be 0.25).
    assert result.price_response < 0.25


def test_cold_taper_reduces_braking_in_deep_cold():
    # Mid tier, full spike. At -15°C outdoor the taper is
    # 0.4 + 0.6 * ((-15 - -20) / (-10 - -20)) = 0.7, so shift = 1.5 * 0.7 = 1.05,
    # vs the full 1.5 at a mild 0°C.
    cold = compute(
        make_inputs(
            current_price=100.0, price_data_available=True, raw_outdoor_temp_c=-15.0
        ),
        make_params(enable_price_compensation=True),
    )
    mild = compute(
        make_inputs(
            current_price=100.0, price_data_available=True, raw_outdoor_temp_c=0.0
        ),
        make_params(enable_price_compensation=True),
    )
    assert cold.cold_taper_factor == pytest.approx(0.7)
    assert cold.price_shift_applied_c == pytest.approx(1.05)
    assert mild.cold_taper_factor == pytest.approx(1.0)
    assert mild.price_shift_applied_c == pytest.approx(1.5)
    assert cold.price_shift_applied_c < mild.price_shift_applied_c


def test_cold_taper_factor_helper_boundaries():
    # 1.0 at/above start, min_factor at/below full, linear between, and a
    # degenerate (start<=full) config disables the taper.
    assert heuristic.cold_taper_factor(-5.0, -10.0, -20.0, 0.4) == 1.0
    assert heuristic.cold_taper_factor(-10.0, -10.0, -20.0, 0.4) == 1.0
    assert heuristic.cold_taper_factor(-20.0, -10.0, -20.0, 0.4) == 0.4
    assert heuristic.cold_taper_factor(-30.0, -10.0, -20.0, 0.4) == 0.4
    assert heuristic.cold_taper_factor(-15.0, -10.0, -20.0, 0.4) == pytest.approx(0.7)
    assert heuristic.cold_taper_factor(-15.0, -20.0, -10.0, 0.4) == 1.0  # degenerate


def test_resolve_price_tier_defaults_to_mid_on_unknown():
    assert heuristic.resolve_price_tier("bogus").max_sag_c == 1.5
    assert heuristic.resolve_price_tier(None).max_sag_c == 1.5
    assert heuristic.resolve_price_tier("HIGH").max_sag_c == 3.0  # case-insensitive


def test_price_explainability_fields_populated():
    result = compute(
        make_inputs(current_price=100.0, price_data_available=True),
        make_params(enable_price_compensation=True, price_comfort_tier="high"),
    )
    assert result.price_comfort_tier == "high"
    assert result.allowed_sag_c == pytest.approx(3.0)
    assert result.upcoming_spike_in_min is None  # Phase B not yet active
    assert result.precharge_active is False


def _spiky_day(peak_hours: float, peak_price: float = 10.0, base: float = 1.0):
    """A 24 h hourly `base` forecast plus a single `peak` entry inserted at
    exactly `peak_hours` from now (supports sub-hour offsets)."""
    entries = [(float(h), base) for h in range(24)]
    entries.append((float(peak_hours), peak_price))
    return tuple(sorted(entries))


def test_flat_day_forecast_suppresses_compensation_entirely():
    # Even at a high absolute price, a forecast with no meaningful spread means
    # "there are no bigger differences today" → leave it alone.
    flat = tuple((float(h), 3.0) for h in range(24))
    result = compute(
        make_inputs(current_price=3.0, price_data_available=True, price_forecast=flat),
        make_params(enable_price_compensation=True),
    )
    assert result.price_shift_applied_c == 0.0
    assert result.price_response == 0.0


def test_relative_band_engages_on_a_spiky_day():
    # Current hour IS the spike; the day's distribution makes it full-authority.
    forecast = _spiky_day(peak_hours=0.0)
    result = compute(
        make_inputs(
            current_price=10.0, price_data_available=True, price_forecast=forecast
        ),
        make_params(enable_price_compensation=True),
    )
    assert result.price_response > 0.5
    assert result.price_shift_applied_c > 0.0
    assert result.upcoming_spike_in_min is None  # the spike is now, not ahead


def test_lookahead_pre_brakes_before_an_upcoming_spike():
    # Cheap right now, but a spike lands in 1 h — within the mid tier's 90 min
    # lead window — so the controller pre-brakes and flags the spike timing.
    forecast = _spiky_day(peak_hours=1.0)
    result = compute(
        make_inputs(
            current_price=1.0, price_data_available=True, price_forecast=forecast
        ),
        make_params(enable_price_compensation=True),
    )
    assert result.price_shift_applied_c > 0.0
    assert result.upcoming_spike_in_min == pytest.approx(60.0)
    assert "pre-braking" in result.reason


def test_lookahead_ignores_spikes_beyond_the_lead_window():
    # Same spike but 5 h out — well past the mid tier's 90 min lead — so no
    # pre-brake yet.
    forecast = _spiky_day(peak_hours=5.0)
    result = compute(
        make_inputs(
            current_price=1.0, price_data_available=True, price_forecast=forecast
        ),
        make_params(enable_price_compensation=True),
    )
    assert result.price_shift_applied_c == 0.0
    assert result.upcoming_spike_in_min is None


def test_pre_brake_ramps_up_as_spike_approaches():
    # Closer spike → stronger pre-brake (proximity weighting).
    near = compute(
        make_inputs(
            current_price=1.0, price_data_available=True, price_forecast=_spiky_day(0.5)
        ),
        make_params(enable_price_compensation=True),
    )
    far = compute(
        make_inputs(
            current_price=1.0, price_data_available=True, price_forecast=_spiky_day(1.0)
        ),
        make_params(enable_price_compensation=True),
    )
    assert near.price_shift_applied_c > far.price_shift_applied_c > 0.0


def test_short_forecast_falls_back_to_absolute_thresholds():
    # Fewer than the minimum points → ignore the distribution, use the user's
    # absolute thresholds on the current price (Phase A behavior).
    short = ((0.0, 5.0), (1.0, 5.0))
    result = compute(
        make_inputs(
            current_price=100.0, price_data_available=True, price_forecast=short
        ),
        make_params(enable_price_compensation=True),
    )
    assert result.price_shift_applied_c == pytest.approx(1.5)  # mid tier full


def test_price_band_thresholds_exposed_for_troubleshooting():
    # A spiky day: the derived engage/full/median thresholds are reported so a
    # "why did/didn't it act" question is answerable from the state alone.
    forecast = _spiky_day(peak_hours=1.0)  # 24×1.0 base + one 10.0
    result = compute(
        make_inputs(
            current_price=1.0, price_data_available=True, price_forecast=forecast
        ),
        make_params(enable_price_compensation=True),
    )
    assert result.price_median == pytest.approx(1.0)
    assert result.price_band_full == pytest.approx(10.0)  # the day's peak
    # engage = median + 0.25 * (peak - median) = 1.0 + 0.25 * 9 = 3.25
    assert result.price_band_start == pytest.approx(3.25)
    assert "band 3.25" in result.reason


def test_flat_day_reports_band_and_no_action_reason():
    flat = tuple((float(h), 3.0) for h in range(24))
    result = compute(
        make_inputs(current_price=3.0, price_data_available=True, price_forecast=flat),
        make_params(enable_price_compensation=True),
    )
    assert result.price_shift_applied_c == 0.0
    assert result.price_median == pytest.approx(3.0)
    assert "flat day" in result.reason


def test_absolute_fallback_band_has_no_median():
    # No forecast → absolute thresholds are the band, and there's no day median.
    result = compute(
        make_inputs(current_price=100.0, price_data_available=True),
        make_params(enable_price_compensation=True),
    )
    assert result.price_median is None
    assert result.price_band_start == pytest.approx(1.5)
    assert result.price_band_full == pytest.approx(3.0)


def test_precharge_preheats_in_cheap_window_before_spike_on_high_tier():
    # High tier, cheap right now (below the day's median), spike coming in 1 h:
    # pre-heat by raising the target (negative shift → the compensated bump goes
    # NEGATIVE, calling for more heat now to bank it).
    forecast = _spiky_day(peak_hours=1.0)
    result = compute(
        make_inputs(
            current_price=1.0, price_data_available=True, price_forecast=forecast
        ),
        make_params(enable_price_compensation=True, price_comfort_tier="high"),
    )
    assert result.precharge_active is True
    assert result.price_shift_applied_c < 0.0  # target raised, not lowered
    assert result.effective_indoor_target_c > result.indoor_target_c
    assert result.price_adjustment_c < 0.0  # more heat now
    assert "pre-charging" in result.reason


def test_precharge_does_not_engage_on_mid_tier():
    # Mid tier opts out of pre-charging: same setup pre-brakes instead.
    forecast = _spiky_day(peak_hours=1.0)
    result = compute(
        make_inputs(
            current_price=1.0, price_data_available=True, price_forecast=forecast
        ),
        make_params(enable_price_compensation=True, price_comfort_tier="mid"),
    )
    assert result.precharge_active is False
    assert result.price_shift_applied_c > 0.0  # braking, not boosting


def test_precharge_bounded_by_comfort_max():
    forecast = _spiky_day(peak_hours=0.5)
    result = compute(
        make_inputs(
            current_price=1.0, price_data_available=True, price_forecast=forecast
        ),
        make_params(
            enable_price_compensation=True,
            price_comfort_tier="high",
            comfort_max_c=21.5,
            precharge_max_boost_c=5.0,  # would blow past comfort_max without the clamp
        ),
    )
    assert result.effective_indoor_target_c == 21.5


def test_precharge_disabled_when_boost_is_zero():
    forecast = _spiky_day(peak_hours=1.0)
    result = compute(
        make_inputs(
            current_price=1.0, price_data_available=True, price_forecast=forecast
        ),
        make_params(
            enable_price_compensation=True,
            price_comfort_tier="high",
            precharge_max_boost_c=0.0,
        ),
    )
    assert result.precharge_active is False


def test_heating_cutoff_engages_at_or_above_threshold():
    result = compute(
        make_inputs(raw_outdoor_temp_c=18.0, indoor_temp_c=19.0, wind_speed_ms=10.0),
        make_params(heating_cutoff_c=18.0),
    )
    assert result.heating_cutoff_engaged is True
    assert result.compensated_outdoor_temp_c == 18.0
    assert result.indoor_adjustment_c == 0.0
    assert result.wind_adjustment_c == 0.0
    assert result.sun_adjustment_c == 0.0
    assert result.price_adjustment_c == 0.0
    assert "heating cutoff" in result.reason


def test_heating_cutoff_not_engaged_below_threshold():
    result = compute(
        make_inputs(raw_outdoor_temp_c=17.9), make_params(heating_cutoff_c=18.0)
    )
    assert result.heating_cutoff_engaged is False


def test_heating_cutoff_ignores_price_even_if_enabled():
    result = compute(
        make_inputs(
            raw_outdoor_temp_c=20.0, current_price=100.0, price_data_available=True
        ),
        make_params(heating_cutoff_c=18.0, enable_price_compensation=True),
    )
    assert result.heating_cutoff_engaged is True
    assert result.current_price is None
    assert result.price_shift_applied_c == 0.0
    assert result.effective_indoor_target_c == result.indoor_target_c


def test_heating_cutoff_still_reports_real_solar_effect():
    # solar_effect is a physical fact the RC shadow model relies on even when
    # the heuristic isn't acting on it — must not be zeroed by the cutoff.
    result = compute(
        make_inputs(
            raw_outdoor_temp_c=25.0,
            sun_elevation_deg=90.0,
            cloud_coverage_pct=0.0,
            cloud_data_available=True,
        ),
        make_params(heating_cutoff_c=18.0),
    )
    assert result.heating_cutoff_engaged is True
    assert result.solar_effect == 1.0
    assert result.sun_adjustment_c == 0.0  # not acted on, but not hidden either
    assert result.wind_data_available is True
    assert result.cloud_data_available is True


# --- optional weather-derived inputs -----------------------------------------


def test_disabled_wind_input_contributes_nothing():
    params = make_params(enable_wind_input=False, k_wind=0.5)
    result = compute(make_inputs(wind_speed_ms=10.0), params)
    assert result.wind_adjustment_c == 0.0
    assert result.compensated_outdoor_temp_c == 3.0
    # The reading itself is still reported — the term is off, the data isn't
    # being hidden.
    assert result.wind_speed_ms == 10.0
    assert "wind input disabled" in result.reason


def test_disabled_solar_input_contributes_nothing():
    params = make_params(enable_solar_input=False, k_sun=3.0)
    result = compute(
        make_inputs(sun_elevation_deg=90.0, cloud_coverage_pct=0.0), params
    )
    assert result.sun_adjustment_c == 0.0
    # solar_effect stays a reported physical fact, as the RC model relies on.
    assert result.solar_effect == 1.0
    assert "solar input disabled" in result.reason


def test_disabled_input_differs_from_unavailable_in_the_reason():
    off = compute(make_inputs(), make_params(enable_wind_input=False))
    missing = compute(make_inputs(wind_data_available=False), make_params())
    assert "disabled" in off.reason
    assert "unavailable" in missing.reason


def test_both_inputs_disabled_leaves_only_indoor_error():
    params = make_params(
        enable_solar_input=False, enable_wind_input=False, k_indoor=2.0
    )
    result = compute(
        make_inputs(
            indoor_temp_c=20.0, wind_speed_ms=8.0, sun_elevation_deg=45.0
        ),
        params,
    )
    # -k_indoor * (21 - 20) = -2.0 on top of raw 3.0
    assert result.compensated_outdoor_temp_c == 1.0


# --- auto-tuned cold-taper override -------------------------------------------


def test_cold_taper_override_replaces_the_outdoor_taper():
    forecast = tuple((float(h), 1.0 if h else 5.0) for h in range(8))
    base = dict(
        enable_price_compensation=True,
        price_comfort_tier="mid",
        k_price=5.0,
        # Manual taper endpoints that would give full authority at -5 degC.
        cold_taper_start_c=-10.0,
        cold_taper_full_c=-20.0,
    )
    inputs = make_inputs(
        raw_outdoor_temp_c=-5.0,
        current_price=5.0,
        price_data_available=True,
        price_forecast=forecast,
    )
    manual = compute(inputs, make_params(**base))
    assert manual.cold_taper_factor == 1.0

    # The override says this house can only recover a quarter of the sag.
    tuned = compute(inputs, make_params(**base, cold_taper_override=0.25))
    assert tuned.cold_taper_factor == 0.25
    assert tuned.price_shift_applied_c < manual.price_shift_applied_c
    assert "recovery-taper" in tuned.reason


def test_cold_taper_override_is_clamped():
    params = make_params(cold_taper_override=7.5)
    assert compute(make_inputs(), params).cold_taper_factor == 1.0
    params = make_params(cold_taper_override=-3.0)
    assert compute(make_inputs(), params).cold_taper_factor == 0.0


def test_no_override_keeps_the_configured_taper():
    params = make_params(
        cold_taper_start_c=-10.0, cold_taper_full_c=-20.0, cold_taper_min_factor=0.4
    )
    result = compute(make_inputs(raw_outdoor_temp_c=-20.0), params)
    assert result.cold_taper_factor == 0.4
