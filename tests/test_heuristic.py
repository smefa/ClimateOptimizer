"""Unit tests for the pure heuristic compute core.

heuristic.py has zero Home Assistant dependency, so it's loaded directly by
file path here rather than via `custom_components.climate_optimizer`, which
would otherwise pull in `homeassistant` through the package's __init__.py.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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


def test_price_between_thresholds_ramps_linearly():
    # threshold_start=1.5, threshold_max=3.0, max_drop=1.0 -> at price 2.25
    # (halfway) the shift should be half of max_drop.
    result = compute(
        make_inputs(current_price=2.25, price_data_available=True),
        make_params(enable_price_compensation=True),
    )
    assert result.price_shift_applied_c == 0.5
    assert result.effective_indoor_target_c == 20.5


def test_price_above_max_threshold_caps_at_max_drop():
    result = compute(
        make_inputs(current_price=100.0, price_data_available=True),
        make_params(enable_price_compensation=True),
    )
    assert result.price_shift_applied_c == 1.0
    assert result.effective_indoor_target_c == 20.0


def test_price_shift_never_exceeds_comfort_min():
    result = compute(
        make_inputs(current_price=100.0, price_data_available=True),
        make_params(
            enable_price_compensation=True,
            price_max_drop_c=10.0,  # would push target to 11.0 without the clamp
            comfort_min_c=18.0,
        ),
    )
    assert result.effective_indoor_target_c == 18.0


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
    assert result.price_shift_applied_c == 1.0
