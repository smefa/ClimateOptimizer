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


# --- Price-compensation comfort tiers -------------------------------------
# The tier is the single knob most users touch. It names how aggressively the
# controller trades comfort for cheaper energy, and bundles four coefficients:
#
#   max_sag_c  – how far indoor is allowed to coast below target during a
#                spike (the compensation is self-limiting: the pump resumes as
#                indoor approaches target - max_sag_c, so this caps the dip).
#   gamma      – convexity of the price→response curve (response = ramp**gamma).
#                gamma > 1 means small price excursions barely register and only
#                a genuine spike bites hard — "react more to larger differences".
#   lead_min   – how far ahead to start pre-braking for pump/emitter lag
#                (used by the Phase B forecast lookahead; inert until then).
#   precharge  – whether pre-heating in the cheap window before a spike is
#                allowed (Phase C; inert until then).
#
# Named Low / Mid / High by *compensation aggressiveness*: Low barely reacts
# (max comfort), High brakes hard (max saving).
PRICE_TIER_LOW = "low"
PRICE_TIER_MID = "mid"
PRICE_TIER_HIGH = "high"


@dataclass(frozen=True)
class PriceTier:
    max_sag_c: float
    gamma: float
    lead_minutes: float
    precharge: bool


PRICE_TIERS: dict[str, PriceTier] = {
    PRICE_TIER_LOW: PriceTier(max_sag_c=0.5, gamma=3.0, lead_minutes=30.0, precharge=False),
    PRICE_TIER_MID: PriceTier(max_sag_c=1.5, gamma=2.0, lead_minutes=90.0, precharge=False),
    PRICE_TIER_HIGH: PriceTier(max_sag_c=3.0, gamma=1.5, lead_minutes=150.0, precharge=True),
}


def resolve_price_tier(name: str | None) -> PriceTier:
    """Map a tier name to its coefficients, defaulting to Mid on anything
    unrecognized so a bad config value degrades gracefully rather than
    disabling compensation."""
    return PRICE_TIERS.get((name or "").strip().lower(), PRICE_TIERS[PRICE_TIER_MID])


def cold_taper_factor(
    outdoor_c: float, start_c: float, full_c: float, min_factor: float
) -> float:
    """Scale braking authority down in deep cold: 1.0 at/above `start_c`,
    `min_factor` at/below `full_c`, linear in between.

    Rationale: at deep-cold temperatures the house loses heat fast *and* the
    heat pump is at its weakest (low COP, reduced output, possible aux/resistive
    top-up), so a hard brake risks a slow, expensive recovery. Brake hard when
    coasting is cheap; brake gently when it's brutal outside. A degenerate
    config (`start_c <= full_c`) disables the taper (returns 1.0) rather than
    dividing by zero."""
    if start_c <= full_c:
        return 1.0
    if outdoor_c >= start_c:
        return 1.0
    if outdoor_c <= full_c:
        return min_factor
    frac = (outdoor_c - full_c) / (start_c - full_c)  # 0 at full_c → 1 at start_c
    return min_factor + (1.0 - min_factor) * frac


# --- Forecast-aware price band + lookahead (Phase B) ----------------------
# Below this many forecast points we don't trust a day-distribution and fall
# back to the user's absolute thresholds.
_MIN_FORECAST_POINTS = 6
# The day must vary by at least this fraction (peak-vs-median, relative to the
# median) before *any* compensation engages — "only compensate when there are
# bigger differences". A flat day gets left alone regardless of tier.
PRICE_MIN_RELATIVE_SPREAD = 0.2
# Where in the median→peak range braking starts engaging. Below this the price
# is "ordinary for today" and the convex curve keeps the response near zero.
_PRICE_ENGAGE_FRACTION = 0.25


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated percentile of an already-sorted list (stdlib only)."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (q / 100.0) * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _price_band_info(
    forecast: tuple[tuple[float, float], ...] | None, params: HeuristicParams
) -> tuple[float, float, float | None, bool]:
    """The price band for this cycle: (engage, full-authority, median, engaged).

    With a usable forecast, derive the thresholds from the day's own
    distribution — the median is "ordinary for today", the peak is full
    authority — so the response self-adapts across seasons and, crucially,
    still catches short (1-2 h) spikes that high percentiles would miss.
    `engaged` is False on a flat day (peak-vs-median spread below the minimum),
    meaning "don't compensate at all" — but the thresholds are still returned
    so a "why didn't it act" question is answerable from the logged state.
    Without a forecast, fall back to the user's absolute thresholds (median
    None, always engaged)."""
    if forecast and len(forecast) >= _MIN_FORECAST_POINTS:
        prices = sorted(p for _, p in forecast)
        p_med = _percentile(prices, 50)
        p_peak = prices[-1]  # the day's actual peak — catches short spikes
        spread = p_peak - p_med
        engaged = spread / max(abs(p_med), 1e-6) >= PRICE_MIN_RELATIVE_SPREAD
        return p_med + _PRICE_ENGAGE_FRACTION * spread, p_peak, p_med, engaged
    return params.price_threshold_start, params.price_threshold_max, None, True


def _band_response(price: float, start: float, full: float, gamma: float) -> float:
    """Convex 0..1 response of a price within the [start, full] band."""
    if full > start:
        ramp = (price - start) / (full - start)
    else:
        # Degenerate band: hard step at `start`.
        ramp = 1.0 if price >= start else 0.0
    return _clamp(ramp, 0.0, 1.0) ** gamma


def _lookahead_response(
    forecast: tuple[tuple[float, float], ...] | None,
    start: float,
    full: float,
    tier: PriceTier,
) -> tuple[float, float | None]:
    """Pre-brake response from an upcoming spike, and how many hours until it.

    Scans the forecast within the tier's lead window and weights each upcoming
    price's response by proximity (weight 1.0 as it arrives, 0.0 at the far
    edge of the window). This ramps braking up *before* a spike so the laggy
    pump/emitters are already coasting down when it hits. Returns the strongest
    weighted response and the hours-ahead of the entry that produced it."""
    if not forecast:
        return 0.0, None
    lead_hours = tier.lead_minutes / 60.0
    if lead_hours <= 0:
        return 0.0, None
    best = 0.0
    best_h: float | None = None
    for t_h, price in forecast:
        if t_h <= 0.0 or t_h > lead_hours:
            continue
        contrib = _band_response(price, start, full, tier.gamma) * (
            1.0 - t_h / lead_hours
        )
        if contrib > best:
            best = contrib
            best_h = t_h
    return best, best_h


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
    # Optional day-ahead price forecast as (hours_from_now, price) pairs. Kept
    # as plain float offsets rather than datetimes so this module stays free of
    # any datetime/HA dependency (the coordinator does the conversion). When
    # present it enables the relative-to-day price band and lookahead
    # pre-braking; when None the controller falls back to absolute thresholds
    # on the current price only (Phase A behavior).
    price_forecast: tuple[tuple[float, float], ...] | None = None


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
    heating_cutoff_c: float
    # --- Price-compensation v2 (all defaulted so older constructions and
    # tests that omit them still build; the coordinator always supplies them).
    # `price_max_drop_c` above is superseded by the tier's `max_sag_c` and is
    # retained only for config back-compat; the v2 path does not read it.
    price_comfort_tier: str = PRICE_TIER_MID
    k_price: float = 5.0
    cold_taper_start_c: float = -10.0
    cold_taper_full_c: float = -20.0
    cold_taper_min_factor: float = 0.4
    # Max target *boost* (°C) when pre-heating in the cheap window before a
    # coming spike (Phase C). Only the High tier pre-charges; set to 0 to
    # disable entirely. Bounded downstream by comfort_max.
    precharge_max_boost_c: float = 1.0
    # --- Optional weather-derived inputs -------------------------------------
    # Each switches off its adjustment term entirely (contributing exactly 0,
    # like an unavailable sensor does) rather than relying on the user zeroing
    # the corresponding k_*. Defaulted True/True so existing constructions and
    # tests that omit them keep the previous always-on behaviour.
    enable_solar_input: bool = True
    enable_wind_input: bool = True
    # --- Auto-tuning override (autotune.py) ----------------------------------
    # When set, replaces the outdoor-temperature cold taper computed from the
    # three `cold_taper_*` values above with a recovery-feasibility factor
    # derived from the learned RC model. None means "use the configured taper",
    # which is what Manual mode and every existing caller get.
    cold_taper_override: float | None = None


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
    heating_cutoff_engaged: bool
    reason: str
    # --- Price-compensation v2 explainability (defaulted; the summer-cutoff
    # branch leaves them at these neutral values since it suppresses price).
    price_comfort_tier: str = PRICE_TIER_MID
    price_response: float = 0.0
    cold_taper_factor: float = 1.0
    allowed_sag_c: float = 0.0
    upcoming_spike_in_min: float | None = None
    precharge_active: bool = False
    # The price band actually in force this cycle, for troubleshooting "why
    # did/didn't it act": the engage and full-authority thresholds and, when a
    # forecast drove them, the day's median. None when price is off/unavailable;
    # median is None when the absolute-threshold fallback was used.
    price_band_start: float | None = None
    price_band_full: float | None = None
    price_median: float | None = None
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
    # solar_effect is a physical fact (how much passive solar gain is
    # happening right now), not a heuristic decision — compute it
    # unconditionally, before the heating-cutoff branch below, so the RC
    # shadow model (which reads it off the result regardless of cutoff
    # state) always sees reality rather than a zeroed value on warm days.
    cloud_fraction = (inputs.cloud_coverage_pct or 0.0) / 100.0
    solar_effect = max(0.0, sin(radians(inputs.sun_elevation_deg))) * (
        1.0 - cloud_fraction
    )

    if inputs.raw_outdoor_temp_c >= params.heating_cutoff_c:
        # Summer guardrail: above the cutoff, suppress compensation
        # entirely rather than letting indoor/wind/price terms push the
        # published value below the raw reading — which could otherwise
        # trick the heat pump's own curve into thinking it's colder than
        # it is and calling for heat on a warm day. Full passthrough, no
        # partial credit for any term.
        return HeuristicResult(
            compensated_outdoor_temp_c=inputs.raw_outdoor_temp_c,
            raw_outdoor_temp_c=inputs.raw_outdoor_temp_c,
            indoor_temp_c=inputs.indoor_temp_c,
            indoor_data_available=inputs.indoor_data_available,
            indoor_target_c=params.indoor_target_c,
            effective_indoor_target_c=params.indoor_target_c,
            indoor_adjustment_c=0.0,
            wind_adjustment_c=0.0,
            sun_adjustment_c=0.0,
            price_adjustment_c=0.0,
            wind_speed_ms=inputs.wind_speed_ms,
            wind_data_available=inputs.wind_data_available,
            cloud_coverage_pct=inputs.cloud_coverage_pct,
            cloud_data_available=inputs.cloud_data_available,
            solar_effect=solar_effect,
            current_price=None,
            price_shift_applied_c=0.0,
            price_data_available=inputs.price_data_available,
            heating_cutoff_engaged=True,
            reason=(
                f"Raw outdoor {inputs.raw_outdoor_temp_c:.1f}°C ≥ heating cutoff "
                f"{params.heating_cutoff_c:.1f}°C; compensation suppressed, "
                f"publishing raw temperature unmodified"
            ),
        )

    if inputs.indoor_data_available and inputs.indoor_temp_c is not None:
        indoor_adjustment_c = -params.k_indoor * (
            params.indoor_target_c - inputs.indoor_temp_c
        )
    else:
        indoor_adjustment_c = 0.0
    # A disabled input contributes exactly 0, identically to an unavailable
    # sensor — the term is off, not merely small.
    wind_adjustment_c = (
        -params.k_wind * inputs.wind_speed_ms if params.enable_wind_input else 0.0
    )
    sun_adjustment_c = params.k_sun * solar_effect if params.enable_solar_input else 0.0

    current_price = (
        inputs.current_price
        if (params.enable_price_compensation and inputs.price_data_available)
        else None
    )

    # --- Price compensation (v2): tiered, convex, cold-tapered ------------
    # 1. A linear ramp of the current price across the [start, max] band, then
    #    2. raised to the tier's gamma so small excursions barely register and
    #    only real spikes bite ("react more to larger price differences"), then
    #    3. scaled by the tier's max sag and the deep-cold taper to get how far
    #    the target is allowed to drop. The resulting compensated-outdoor bump
    #    is `k_price * price_shift_c` — decoupled from k_indoor so braking
    #    authority can be tuned independently and much harder.
    tier = resolve_price_tier(params.price_comfort_tier)
    if params.cold_taper_override is not None:
        # Auto-tuned: a recovery-feasibility factor derived from the learned RC
        # model (see autotune.derived_cold_taper), which reproduces the taper's
        # intent — "don't spend comfort you can't buy back" — from this house's
        # own physics rather than from three hand-drawn outdoor thresholds.
        taper = _clamp(params.cold_taper_override, 0.0, 1.0)
    else:
        taper = cold_taper_factor(
            inputs.raw_outdoor_temp_c,
            params.cold_taper_start_c,
            params.cold_taper_full_c,
            params.cold_taper_min_factor,
        )
    price_response = 0.0
    price_shift_c = 0.0
    upcoming_spike_in_min: float | None = None
    precharge_active = False
    price_band_start: float | None = None
    price_band_full: float | None = None
    price_median: float | None = None
    price_flat_day = False
    if current_price is not None:
        start, full, price_median, band_engaged = _price_band_info(
            inputs.price_forecast, params
        )
        price_band_start, price_band_full = start, full
        price_flat_day = not band_engaged
        if band_engaged:
            response_now = _band_response(current_price, start, full, tier.gamma)
            response_lead, spike_h = _lookahead_response(
                inputs.price_forecast, start, full, tier
            )
            spike_ahead_min = None if spike_h is None else spike_h * 60.0

            # Pre-charge (Phase C) and braking are mutually exclusive and picked
            # by the *current* price: if it's genuinely cheap now (at/below the
            # day's median) with a spike coming and the tier opts in, bank heat
            # instead of cutting it — a negative shift (target boost) scaled by
            # the spike's imminence, bounded by comfort_max downstream. It makes
            # no sense to pre-brake (coast into a spike already cold) while it's
            # still cheap; pre-braking is for when the price is already elevated.
            precharge_ready = (
                tier.precharge
                and params.precharge_max_boost_c > 0.0
                and response_lead > 0.0
                and price_median is not None
                and current_price <= price_median
            )
            if precharge_ready:
                price_shift_c = -params.precharge_max_boost_c * response_lead
                precharge_active = True
                upcoming_spike_in_min = spike_ahead_min
            else:
                # Brake now, or pre-brake if the coming spike outweighs the
                # current price.
                if response_lead > response_now:
                    price_response = response_lead
                    upcoming_spike_in_min = spike_ahead_min
                else:
                    price_response = response_now
                price_shift_c = price_response * tier.max_sag_c * taper

    effective_indoor_target_c = _clamp(
        params.indoor_target_c - price_shift_c,
        params.comfort_min_c,
        params.comfort_max_c,
    )
    price_adjustment_c = -params.k_price * (
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
    if not params.enable_wind_input:
        reason += "wind input disabled; "
    elif inputs.wind_data_available:
        reason += f"wind {inputs.wind_speed_ms:.1f} m/s → {wind_adjustment_c:+.1f}°C; "
    else:
        reason += "wind forecast unavailable, treated as calm; "
    if not params.enable_solar_input:
        reason += "solar input disabled"
    elif inputs.cloud_data_available:
        reason += f"sun {solar_effect * 100:.0f}% → {sun_adjustment_c:+.1f}°C"
    else:
        reason += f"cloud/sun forecast unavailable, assumed clear sky → {sun_adjustment_c:+.1f}°C"
    if current_price is not None:
        reason += (
            f"; price {current_price:.2f} [{tier.max_sag_c:.1f}°C '{params.price_comfort_tier}' tier"
        )
        if price_band_start is not None and price_band_full is not None:
            reason += f", band {price_band_start:.2f}–{price_band_full:.2f}"
        if price_flat_day:
            reason += ", flat day → no price action"
        if taper < 1.0:
            taper_source = (
                "recovery-taper" if params.cold_taper_override is not None
                else "cold-taper"
            )
            reason += f", {taper_source} ×{taper:.2f}"
        if precharge_active:
            reason += (
                f", pre-charging (cheap) ahead of spike in {upcoming_spike_in_min:.0f} min"
            )
        elif upcoming_spike_in_min is not None:
            reason += f", pre-braking for spike in {upcoming_spike_in_min:.0f} min"
        reason += (
            f"] → target {effective_indoor_target_c:.1f}°C → {price_adjustment_c:+.1f}°C"
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
        heating_cutoff_engaged=False,
        reason=reason,
        price_comfort_tier=params.price_comfort_tier,
        price_response=price_response,
        cold_taper_factor=taper,
        allowed_sag_c=tier.max_sag_c * taper,
        upcoming_spike_in_min=upcoming_spike_in_min,
        precharge_active=precharge_active,
        price_band_start=price_band_start,
        price_band_full=price_band_full,
        price_median=price_median,
    )
