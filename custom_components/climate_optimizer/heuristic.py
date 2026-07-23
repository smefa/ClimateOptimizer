"""Pure compute core for ClimateOptimizer.

No imports beyond the standard library and no dependency on any other module
in this package (not even `const.py`) on purpose: this module is meant to be
importable and unit-testable in complete isolation, without Home Assistant
installed, and is the seam where a future physics-based (RC network) thermal
model with a proper multi-hour cost-optimizing controller can replace this
heuristic without touching the coordinator or entity code.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import radians, sin

MODEL_VERSION = "heuristic_v1"

# Safety bounds on the final output, independent of user-configured comfort
# bounds (which constrain the *target*, not the output) — a last-resort guard
# against garbage propagating downstream if an upstream source misbehaves.
OUTPUT_SANITY_MIN_C = -40.0
OUTPUT_SANITY_MAX_C = 25.0


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class HeuristicInputs:
    """Live values gathered by the coordinator for one compute cycle."""

    indoor_temp_c: float | None
    indoor_data_available: bool
    raw_outdoor_temp_c: float
    wind_speed_ms: float
    wind_data_available: bool
    sun_elevation_deg: float
    cloud_coverage_pct: float | None
    cloud_data_available: bool
    current_price: float | None
    price_data_available: bool


@dataclass(frozen=True)
class HeuristicParams:
    """User-tunable coefficients, sourced from the config entry's options."""

    indoor_target_c: float
    enable_price_compensation: bool
    k_indoor: float
    k_wind: float
    k_sun: float
    comfort_min_c: float
    comfort_max_c: float
    price_threshold_start: float
    price_threshold_max: float
    price_max_drop_c: float


@dataclass(frozen=True)
class HeuristicResult:
    """The full explainable output. Doubles as the sensor's attribute schema."""

    compensated_outdoor_temp_c: float
    raw_outdoor_temp_c: float
    indoor_temp_c: float | None
    indoor_data_available: bool
    indoor_target_c: float
    effective_indoor_target_c: float
    indoor_adjustment_c: float
    wind_adjustment_c: float
    sun_adjustment_c: float
    price_adjustment_c: float
    wind_speed_ms: float
    wind_data_available: bool
    cloud_coverage_pct: float | None
    cloud_data_available: bool
    solar_effect: float
    current_price: float | None
    price_shift_applied_c: float
    price_data_available: bool
    reason: str
    model_version: str = MODEL_VERSION
    # Reserved for a future RC-model/MPC controller. Always None for the
    # heuristic (Phase 1) so the attribute schema is stable for consumers
    # ahead of that model actually populating them.
    predicted_trajectory: list | None = None
    binding_constraint: str | None = None


def compute(inputs: HeuristicInputs, params: HeuristicParams) -> HeuristicResult:
    """Compute the compensated outdoor temperature and its explanation.

    If the indoor sensor is unavailable, the indoor-error term is skipped
    (contributes 0) rather than failing the whole computation — the result
    falls back toward the raw outdoor temperature (plus whatever other terms
    are still available), which is a safe default: it's the same "no
    compensation" behavior the heat pump's own curve would apply on its own.
    """
    if inputs.indoor_data_available and inputs.indoor_temp_c is not None:
        indoor_adjustment_c = -params.k_indoor * (
            params.indoor_target_c - inputs.indoor_temp_c
        )
    else:
        indoor_adjustment_c = 0.0
    wind_adjustment_c = -params.k_wind * inputs.wind_speed_ms

    cloud_fraction = (inputs.cloud_coverage_pct or 0.0) / 100.0
    solar_effect = max(0.0, sin(radians(inputs.sun_elevation_deg))) * (
        1.0 - cloud_fraction
    )
    sun_adjustment_c = params.k_sun * solar_effect

    current_price = (
        inputs.current_price
        if (params.enable_price_compensation and inputs.price_data_available)
        else None
    )

    price_shift_c = 0.0
    if current_price is not None:
        span = params.price_threshold_max - params.price_threshold_start
        if span > 0:
            ramp = (current_price - params.price_threshold_start) / span
        else:
            # Degenerate config (max <= start): treat as a hard step at start.
            ramp = 1.0 if current_price >= params.price_threshold_start else 0.0
        price_shift_c = _clamp(ramp, 0.0, 1.0) * params.price_max_drop_c

    effective_indoor_target_c = _clamp(
        params.indoor_target_c - price_shift_c,
        params.comfort_min_c,
        params.comfort_max_c,
    )
    price_adjustment_c = -params.k_indoor * (
        effective_indoor_target_c - params.indoor_target_c
    )

    compensated_outdoor_temp_c = _clamp(
        inputs.raw_outdoor_temp_c
        + indoor_adjustment_c
        + price_adjustment_c
        + wind_adjustment_c
        + sun_adjustment_c,
        OUTPUT_SANITY_MIN_C,
        OUTPUT_SANITY_MAX_C,
    )

    if inputs.indoor_data_available and inputs.indoor_temp_c is not None:
        reason = (
            f"Indoor {inputs.indoor_temp_c:.1f}°C vs target {params.indoor_target_c:.1f}°C "
            f"→ {indoor_adjustment_c:+.1f}°C; "
        )
    else:
        reason = "Indoor sensor unavailable, compensation skipped for this term; "
    if inputs.wind_data_available:
        reason += f"wind {inputs.wind_speed_ms:.1f} m/s → {wind_adjustment_c:+.1f}°C; "
    else:
        reason += "wind forecast unavailable, treated as calm; "
    if inputs.cloud_data_available:
        reason += f"sun {solar_effect * 100:.0f}% → {sun_adjustment_c:+.1f}°C"
    else:
        reason += f"cloud/sun forecast unavailable, assumed clear sky → {sun_adjustment_c:+.1f}°C"
    if current_price is not None:
        reason += (
            f"; price {current_price:.2f} → target {effective_indoor_target_c:.1f}°C "
            f"→ {price_adjustment_c:+.1f}°C"
        )
    reason += (
        f"; total {compensated_outdoor_temp_c - inputs.raw_outdoor_temp_c:+.1f}°C "
        f"from raw {inputs.raw_outdoor_temp_c:.1f}°C"
    )

    return HeuristicResult(
        compensated_outdoor_temp_c=compensated_outdoor_temp_c,
        raw_outdoor_temp_c=inputs.raw_outdoor_temp_c,
        indoor_temp_c=inputs.indoor_temp_c,
        indoor_data_available=inputs.indoor_data_available,
        indoor_target_c=params.indoor_target_c,
        effective_indoor_target_c=effective_indoor_target_c,
        indoor_adjustment_c=indoor_adjustment_c,
        wind_adjustment_c=wind_adjustment_c,
        sun_adjustment_c=sun_adjustment_c,
        price_adjustment_c=price_adjustment_c,
        wind_speed_ms=inputs.wind_speed_ms,
        wind_data_available=inputs.wind_data_available,
        cloud_coverage_pct=inputs.cloud_coverage_pct,
        cloud_data_available=inputs.cloud_data_available,
        solar_effect=solar_effect,
        current_price=current_price,
        price_shift_applied_c=price_shift_c,
        price_data_available=inputs.price_data_available,
        reason=reason,
    )
