"""Composes the final compensated outdoor temperature from its terms.

Pure module: standard library only and no dependency on any other module in
this package (not even `const.py`), so it stays importable and unit-testable
with zero Home Assistant dependency.

## What this module is, and is no longer

It used to be the whole controller: a proportional term on indoor error with a
hand-typed gain, plus weather and price corrections. The proportional term has
moved to `learner.py`, which measures what offset this house actually needs
instead of scaling an error by a number somebody guessed. What remains here is
composition and the price logic:

    compensated = raw_outdoor
                + learned_offset      from learner.py (integral + small P)
                - wind * WIND_GAIN    feedforward, optional
                + solar * SOLAR_GAIN  feedforward, optional
                + price_adjustment    bounded, tier-scaled, timed off the
                                      measured fall time

## Why the two feedforward gains are constants and not config

Solar and wind vary far faster than the learner's integrator can track, so they
have to be fed forward rather than learned out — that is what feedforward is
for. But they are also NOT worth learning: the 2026-08-07 validation fitted a
solar coefficient that came out negative and significant, i.e. sunlight cooling
the house, because sun correlates with outdoor temperature and the regression
happily attributed envelope behaviour to it. Wind is worse; it is collinear
with the envelope term outright.

That negative fit has a second cause worth recording, because it bounds how far
the result generalises: the indoor sensor in that house was in the basement.
There was no solar gain at the measurement point to recover, so the only
sun-shaped signal in the data was the correlation with cold, clear weather.
Both effects push the coefficient the same way, and neither is fixed by
collecting more data or by running open loop — the sun-and-cold correlation is
a property of the weather, not of the control loop.

The practical consequence is a placement rule rather than a modelling one, and
it lives in the README and the config-flow help: enable the solar term only
when the indoor sensor is somewhere the sun actually reaches, and the wind term
only for a genuinely draughty building. Both default off. Whether the solar
gain is large is a per-house question — in a house with the sensor in the sun's
path it can dominate a winter afternoon, which is exactly the case the fixed
SOLAR_GAIN_C is a rough compromise for.

A fixed, physically-sensible constant has a much better failure mode. Any bias
in the daily average is absorbed by the integrator within a day, so a slightly
wrong gain causes a little intra-day ripple and no steady-state error at all.
A learned gain with the wrong sign would actively fight the loop.

## Price: a deliberate excursion, not a disturbance

Price compensation is the one thing here that intentionally holds the house
away from target. That matters for the learner, which would otherwise read the
sag as error and wind up fighting it. `price_braking` on the result is what
tells the learner to freeze; see `learner._freeze_reason`.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import radians, sin

MODEL_VERSION = "compose_v2"

# Safety bounds on the final output, independent of anything the user or the
# learner can set — a last-resort guard against garbage propagating to a real
# heat pump if an upstream source misbehaves.
OUTPUT_SANITY_MIN_C = -40.0
OUTPUT_SANITY_MAX_C = 25.0

# Feedforward gains for the two optional weather inputs. See the module
# docstring for why these are constants rather than configuration or learned
# parameters. Units: degC of outdoor spoof per unit of input.
SOLAR_GAIN_C = 3.0
WIND_GAIN_C_PER_MS = 0.3

# A price adjustment smaller than this is not a deliberate excursion and should
# not freeze the learner.
PRICE_BRAKING_EPS_C = 0.05


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def solar_effect_of(sun_elevation_deg: float, cloud_coverage_pct: float | None) -> float:
    """Fraction (0..1) of full solar gain available right now.

    Pure geometry and weather, no occupant preference in it: zero at or below
    the horizon, scaling up with sun height and clear sky above it. Missing
    cloud data is treated as clear (`cloud_coverage_pct` is None -> 0%
    coverage), the same "assume clear" behaviour `compute()` already reported
    in its reason string. Extracted as a named function, rather than left
    inline in `compute()`, so the coordinator can reuse this exact formula —
    e.g. to solar-correct the baseline learner's samples — without a second
    copy that could drift out of sync.
    """
    cloud_fraction = (cloud_coverage_pct or 0.0) / 100.0
    return max(0.0, sin(radians(sun_elevation_deg))) * (1.0 - cloud_fraction)


# --- Price comfort tiers ---------------------------------------------------
# The single knob most users touch: how much comfort to trade for cheaper
# energy. Named by aggressiveness — Low barely reacts (max comfort), High
# brakes hard (max saving).
#
#   max_sag_c       how far indoor may coast below target during a spike. Also
#                   the sag the recovery-feasibility taper is measured against.
#   gamma           convexity of the price->response curve (response = ramp**gamma).
#                   Above 1, small excursions barely register and only a real
#                   spike bites.
#   precharge_c     how far above target heat may be banked in a cheap window
#                   ahead of a spike. Zero disables pre-charging for that tier.
#
# Note what is NOT here any more: a lead time. How early to act is a property
# of the house's emitters, not of how hard the occupant wants to chase price,
# and it is now measured (lag.py) rather than tiered.
PRICE_TIER_LOW = "low"
PRICE_TIER_MID = "mid"
PRICE_TIER_HIGH = "high"


@dataclass(frozen=True)
class PriceTier:
    max_sag_c: float
    gamma: float
    precharge_c: float


PRICE_TIERS: dict[str, PriceTier] = {
    PRICE_TIER_LOW: PriceTier(max_sag_c=0.5, gamma=3.0, precharge_c=0.0),
    PRICE_TIER_MID: PriceTier(max_sag_c=1.5, gamma=2.0, precharge_c=0.0),
    PRICE_TIER_HIGH: PriceTier(max_sag_c=3.0, gamma=1.5, precharge_c=1.0),
}


def resolve_price_tier(name: str | None) -> PriceTier:
    """Map a tier name to its coefficients, defaulting to Mid on anything
    unrecognized so a bad value degrades gracefully rather than disabling
    compensation."""
    return PRICE_TIERS.get((name or "").strip().lower(), PRICE_TIERS[PRICE_TIER_MID])


# --- Cold caution ----------------------------------------------------------
# The one genuinely manual control over deep-cold behaviour, and deliberately
# manual: braking is cheap to enter and expensive to exit, because recovering
# a sag at -15 degC is slow and may run through resistive backup heat at a
# terrible coefficient of performance. That is a risk-appetite question about
# money versus comfort, not a measurable fact about the building, so it is the
# occupant's to answer.
#
#   floor_c      below this outdoor temperature, no price braking at all.
#   exponent     how sharply the measured recovery-feasibility factor bites.
#                Above 1 tapers harder, below 1 more gently.
COLD_CAUTION_LOW = "low"
COLD_CAUTION_MID = "mid"
COLD_CAUTION_HIGH = "high"

# Over how many degrees above the floor braking authority ramps back to full.
COLD_CAUTION_RAMP_C = 8.0


@dataclass(frozen=True)
class ColdCaution:
    floor_c: float
    exponent: float


COLD_CAUTIONS: dict[str, ColdCaution] = {
    COLD_CAUTION_LOW: ColdCaution(floor_c=-20.0, exponent=0.5),
    COLD_CAUTION_MID: ColdCaution(floor_c=-10.0, exponent=1.0),
    COLD_CAUTION_HIGH: ColdCaution(floor_c=-5.0, exponent=2.0),
}


def resolve_cold_caution(name: str | None) -> ColdCaution:
    return COLD_CAUTIONS.get(
        (name or "").strip().lower(), COLD_CAUTIONS[COLD_CAUTION_MID]
    )


def cold_brake_factor(
    outdoor_c: float,
    caution: ColdCaution,
    recoverable_sag_c: float,
    tier_max_sag_c: float,
) -> float:
    """How much price-braking authority survives the cold, in [0, 1].

    Two independent reductions, multiplied:

    1. The occupant's caution setting. Hard zero at or below `floor_c`, ramping
       linearly back to full over `COLD_CAUTION_RAMP_C` degrees above it. This
       works from the first cycle on a fresh install, with nothing learned.

    2. Measured recovery feasibility. Once the current outdoor bin has observed
       how fast it actually closes an error at high offset, the sag is limited
       to what can genuinely be bought back — the question the old three-knob
       hand-drawn taper was approximating. With no measurement yet this
       contributes 1.0, i.e. it does not taper on evidence it does not have,
       leaving the caution ramp in sole charge.
    """
    if outdoor_c <= caution.floor_c:
        return 0.0
    ramp = _clamp((outdoor_c - caution.floor_c) / COLD_CAUTION_RAMP_C, 0.0, 1.0)
    if recoverable_sag_c > 0.0 and tier_max_sag_c > 0.0:
        feasibility = _clamp(recoverable_sag_c / tier_max_sag_c, 0.0, 1.0)
    else:
        feasibility = 1.0
    return _clamp(ramp * (feasibility**caution.exponent), 0.0, 1.0)


# --- Forecast-relative price band ------------------------------------------
# Below this many forecast points there is no usable day-distribution.
_MIN_FORECAST_POINTS = 6
# The day must vary by at least this fraction (peak vs median, relative to the
# median) before any compensation engages. A flat day gets left alone.
PRICE_MIN_RELATIVE_SPREAD = 0.2
# Where in the median->peak range braking starts. Below this the price is
# "ordinary for today" and the convex curve keeps the response near zero.
_PRICE_ENGAGE_FRACTION = 0.25


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated percentile of an already-sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (q / 100.0) * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _price_band_info(
    forecast: tuple[tuple[float, float], ...] | None,
) -> tuple[float, float, float, bool] | None:
    """The price band for this cycle: (engage, full authority, median, engaged).

    Derived from the day's own distribution — median is "ordinary for today",
    peak is full authority — so the response self-adapts across seasons and
    still catches short one- and two-hour spikes that a high percentile would
    miss. `engaged` is False on a flat day.

    Returns None when there is no usable forecast, which means "do nothing".
    That is deliberately not the same as the absolute-threshold fallback this
    replaced: without a day-distribution there is no principled way to know
    whether today's price is high, and guessing from two configured constants
    was both a config burden and a way to brake on an ordinary day.
    """
    if not forecast or len(forecast) < _MIN_FORECAST_POINTS:
        return None
    prices = sorted(p for _, p in forecast)
    p_med = _percentile(prices, 50)
    p_peak = prices[-1]  # the day's actual peak — catches short spikes
    spread = p_peak - p_med
    engaged = spread / max(abs(p_med), 1e-6) >= PRICE_MIN_RELATIVE_SPREAD
    return p_med + _PRICE_ENGAGE_FRACTION * spread, p_peak, p_med, engaged


def _band_response(price: float, start: float, full: float, gamma: float) -> float:
    """Convex 0..1 response of a price within the [start, full] band."""
    if full > start:
        ramp = (price - start) / (full - start)
    else:
        ramp = 1.0 if price >= start else 0.0
    return _clamp(ramp, 0.0, 1.0) ** gamma


def _lookahead_response(
    forecast: tuple[tuple[float, float], ...] | None,
    start: float,
    full: float,
    gamma: float,
    lead_minutes: float,
) -> tuple[float, float | None]:
    """Pre-brake response from an upcoming spike, and how many hours until it.

    Scans the forecast within `lead_minutes` — the MEASURED fall time, i.e. how
    long this house keeps gaining heat after the pump stops calling for it —
    and weights each upcoming price by proximity. Braking that starts later
    than that is still pushing heat into the hour it was meant to avoid, which
    is exactly what the old fixed tier constants (30/90/150 min) did on any
    slab.
    """
    if not forecast:
        return 0.0, None
    lead_hours = lead_minutes / 60.0
    if lead_hours <= 0:
        return 0.0, None
    best = 0.0
    best_h: float | None = None
    for t_h, price in forecast:
        if t_h <= 0.0 or t_h > lead_hours:
            continue
        contrib = _band_response(price, start, full, gamma) * (1.0 - t_h / lead_hours)
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
    # Day-ahead price as (hours_from_now, price) pairs. Plain float offsets
    # rather than datetimes so this module stays free of any datetime or HA
    # dependency; the coordinator does the conversion.
    price_forecast: tuple[tuple[float, float], ...] | None = None
    # The learned offset from learner.py. This is the whole indoor-error
    # response — there is no separate proportional gain here any more.
    learned_offset_c: float = 0.0
    # Measured fall time (lag.py), in minutes: how long this house keeps
    # gaining heat after the pump backs off. Sets the pre-brake lead.
    fall_minutes: float = 120.0
    # Measured rise time, in minutes. Sets the pre-charge lead — banked heat
    # must have ARRIVED before the spike, not still be on its way.
    rise_minutes: float = 90.0
    # How much sag the current outdoor bin has been observed to buy back within
    # the recovery window. 0.0 means "no measurement yet".
    recoverable_sag_c: float = 0.0


@dataclass(frozen=True)
class HeuristicParams:
    """Occupant preferences and resolved settings for one cycle.

    Every field here is either something the occupant chose or something the
    coordinator derived. None of it is a control gain: there are no k_* values
    left to type in.
    """

    indoor_target_c: float
    # Absolute indoor floor for price compensation — the only comfort bound
    # left, and it applies to nothing else.
    comfort_min_c: float
    heating_cutoff_c: float
    enable_price_compensation: bool
    price_comfort_tier: str = PRICE_TIER_MID
    cold_caution: str = COLD_CAUTION_MID
    enable_solar_input: bool = True
    enable_wind_input: bool = True
    # Degrees of outdoor spoof per degree of steady indoor change, supplied by
    # the coordinator from learner.SPOOF_PER_INDOOR_C so the constant is
    # single-sourced without this module importing a sibling.
    spoof_per_indoor_c: float = 1.0


@dataclass(frozen=True)
class HeuristicResult:
    """The full explainable output. Doubles as the sensor's attribute schema."""

    compensated_outdoor_temp_c: float
    raw_outdoor_temp_c: float
    indoor_temp_c: float | None
    indoor_data_available: bool
    indoor_target_c: float
    effective_indoor_target_c: float
    learned_offset_c: float
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
    price_comfort_tier: str = PRICE_TIER_MID
    cold_caution: str = COLD_CAUTION_MID
    price_response: float = 0.0
    cold_brake_factor: float = 1.0
    allowed_sag_c: float = 0.0
    upcoming_spike_in_min: float | None = None
    precharge_active: bool = False
    # True while price compensation is deliberately holding the house away from
    # target. Read by the learner, which must not treat that as error to be
    # integrated away.
    price_braking: bool = False
    lead_minutes_effective: float = 0.0
    price_band_start: float | None = None
    price_band_full: float | None = None
    price_median: float | None = None
    model_version: str = MODEL_VERSION


def compute(inputs: HeuristicInputs, params: HeuristicParams) -> HeuristicResult:
    """Compose the compensated outdoor temperature and its explanation."""
    # Solar effect is a physical fact, not a control decision, so it is computed
    # unconditionally — before the cutoff branch — because the learner and the
    # logs want reality rather than a zeroed value on warm days.
    solar_effect = solar_effect_of(inputs.sun_elevation_deg, inputs.cloud_coverage_pct)

    if inputs.raw_outdoor_temp_c >= params.heating_cutoff_c:
        # Summer guardrail: above the cutoff, suppress everything rather than
        # letting a cold indoor reading or a windy afternoon push the published
        # value below the raw one and trick the pump's curve into calling for
        # heat on a warm day. Full passthrough, no partial credit.
        return HeuristicResult(
            compensated_outdoor_temp_c=inputs.raw_outdoor_temp_c,
            raw_outdoor_temp_c=inputs.raw_outdoor_temp_c,
            indoor_temp_c=inputs.indoor_temp_c,
            indoor_data_available=inputs.indoor_data_available,
            indoor_target_c=params.indoor_target_c,
            effective_indoor_target_c=params.indoor_target_c,
            learned_offset_c=0.0,
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
            price_comfort_tier=params.price_comfort_tier,
            cold_caution=params.cold_caution,
            reason=(
                f"Raw outdoor {inputs.raw_outdoor_temp_c:.1f}°C ≥ heating cutoff "
                f"{params.heating_cutoff_c:.1f}°C; compensation suppressed, "
                f"publishing raw temperature unmodified"
            ),
        )

    # A disabled input contributes exactly 0, identically to an unavailable
    # sensor — the term is off, not merely small.
    wind_adjustment_c = (
        -WIND_GAIN_C_PER_MS * inputs.wind_speed_ms if params.enable_wind_input else 0.0
    )
    sun_adjustment_c = SOLAR_GAIN_C * solar_effect if params.enable_solar_input else 0.0

    # The raw price feed is informational and never gated behind the
    # compensation toggle — a house with braking switched off can still ask
    # "what's it cost right now?". `price_for_braking` is the one that feeds
    # the response math below, and stays strictly gated: that's the value
    # that would actually move the published temperature.
    current_price = inputs.current_price if inputs.price_data_available else None
    price_for_braking = current_price if params.enable_price_compensation else None

    tier = resolve_price_tier(params.price_comfort_tier)
    caution = resolve_cold_caution(params.cold_caution)
    taper = cold_brake_factor(
        inputs.raw_outdoor_temp_c, caution, inputs.recoverable_sag_c, tier.max_sag_c
    )

    price_response = 0.0
    price_shift_c = 0.0
    upcoming_spike_in_min: float | None = None
    precharge_active = False
    price_band_start: float | None = None
    price_band_full: float | None = None
    price_median: float | None = None
    price_flat_day = False
    no_forecast = False
    # Pre-braking is timed off the fall time and pre-charging off the rise
    # time: they are asking different questions of the same plant.
    lead_minutes = max(0.0, inputs.fall_minutes)
    precharge_lead_minutes = max(0.0, inputs.rise_minutes)

    if current_price is not None:
        band = _price_band_info(inputs.price_forecast)
        if band is None:
            no_forecast = True
        else:
            start, full, price_median, band_engaged = band
            # Everything past this point is braking behaviour, not display —
            # stays gated on price_for_braking so a house with compensation
            # off gets the median for free but never a brake threshold.
            if price_for_braking is not None:
                price_band_start, price_band_full = start, full
                price_flat_day = not band_engaged
                if band_engaged:
                    response_now = _band_response(price_for_braking, start, full, tier.gamma)
                    response_brake, brake_h = _lookahead_response(
                        inputs.price_forecast, start, full, tier.gamma, lead_minutes
                    )
                    response_charge, charge_h = _lookahead_response(
                        inputs.price_forecast,
                        start,
                        full,
                        tier.gamma,
                        precharge_lead_minutes,
                    )

                    # Pre-charge and braking are mutually exclusive, chosen by
                    # the CURRENT price: if it is genuinely cheap now (at or
                    # below the day's median) with a spike coming and the tier
                    # opts in, bank heat instead of cutting it. Pre-braking
                    # into a spike while the price is still cheap would just
                    # make the house cold for no saving.
                    precharge_ready = (
                        tier.precharge_c > 0.0
                        and response_charge > 0.0
                        and price_for_braking <= price_median
                    )
                    if precharge_ready:
                        price_shift_c = -tier.precharge_c * response_charge
                        precharge_active = True
                        upcoming_spike_in_min = (
                            None if charge_h is None else charge_h * 60.0
                        )
                    else:
                        if response_brake > response_now:
                            price_response = response_brake
                            upcoming_spike_in_min = (
                                None if brake_h is None else brake_h * 60.0
                            )
                        else:
                            price_response = response_now
                        price_shift_c = price_response * tier.max_sag_c * taper

    # The comfort floor is the only bound applied here. There is no upper
    # clamp: pre-charging is already limited by the tier's own boost, so a
    # second bound above never bound anything.
    effective_indoor_target_c = params.indoor_target_c - price_shift_c
    if effective_indoor_target_c < params.comfort_min_c:
        effective_indoor_target_c = params.comfort_min_c
    # Recompute from the clamped target so the reported shift is what is really
    # being asked for, not what was asked for before the floor bit.
    applied_shift_c = params.indoor_target_c - effective_indoor_target_c
    price_adjustment_c = applied_shift_c * params.spoof_per_indoor_c

    compensated_outdoor_temp_c = _clamp(
        inputs.raw_outdoor_temp_c
        + inputs.learned_offset_c
        + price_adjustment_c
        + wind_adjustment_c
        + sun_adjustment_c,
        OUTPUT_SANITY_MIN_C,
        OUTPUT_SANITY_MAX_C,
    )

    if inputs.indoor_data_available and inputs.indoor_temp_c is not None:
        reason = (
            f"Indoor {inputs.indoor_temp_c:.1f}°C vs target "
            f"{params.indoor_target_c:.1f}°C → learned offset "
            f"{inputs.learned_offset_c:+.2f}°C; "
        )
    else:
        reason = (
            f"Indoor sensor unavailable, holding learned offset "
            f"{inputs.learned_offset_c:+.2f}°C; "
        )
    if not params.enable_wind_input:
        reason += "wind off; "
    elif inputs.wind_data_available:
        reason += f"wind {inputs.wind_speed_ms:.1f} m/s → {wind_adjustment_c:+.1f}°C; "
    else:
        reason += "wind forecast unavailable, treated as calm; "
    if not params.enable_solar_input:
        reason += "solar off"
    elif inputs.cloud_data_available:
        reason += f"sun {solar_effect * 100:.0f}% → {sun_adjustment_c:+.1f}°C"
    else:
        reason += f"cloud/sun forecast unavailable, assumed clear → {sun_adjustment_c:+.1f}°C"
    if price_for_braking is not None:
        reason += f"; price {price_for_braking:.2f} ['{params.price_comfort_tier}' tier"
        if no_forecast:
            reason += ", no day-ahead forecast → no price action"
        if price_band_start is not None and price_band_full is not None:
            reason += f", band {price_band_start:.2f}–{price_band_full:.2f}"
        if price_flat_day:
            reason += ", flat day → no price action"
        if taper < 1.0 and not no_forecast and not price_flat_day:
            if taper <= 0.0:
                reason += (
                    f", too cold to brake ('{params.cold_caution}' cold caution)"
                )
            else:
                reason += f", cold caution ×{taper:.2f}"
        if precharge_active:
            reason += (
                f", pre-charging ahead of spike in {upcoming_spike_in_min:.0f} min "
                f"({precharge_lead_minutes:.0f} min rise)"
            )
        elif upcoming_spike_in_min is not None:
            reason += (
                f", pre-braking for spike in {upcoming_spike_in_min:.0f} min "
                f"({lead_minutes:.0f} min fall)"
            )
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
        learned_offset_c=inputs.learned_offset_c,
        wind_adjustment_c=wind_adjustment_c,
        sun_adjustment_c=sun_adjustment_c,
        price_adjustment_c=price_adjustment_c,
        wind_speed_ms=inputs.wind_speed_ms,
        wind_data_available=inputs.wind_data_available,
        cloud_coverage_pct=inputs.cloud_coverage_pct,
        cloud_data_available=inputs.cloud_data_available,
        solar_effect=solar_effect,
        current_price=current_price,
        price_shift_applied_c=applied_shift_c,
        price_data_available=inputs.price_data_available,
        heating_cutoff_engaged=False,
        reason=reason,
        price_comfort_tier=params.price_comfort_tier,
        cold_caution=params.cold_caution,
        price_response=price_response,
        cold_brake_factor=taper,
        allowed_sag_c=tier.max_sag_c * taper,
        upcoming_spike_in_min=upcoming_spike_in_min,
        precharge_active=precharge_active,
        price_braking=abs(applied_shift_c) > PRICE_BRAKING_EPS_C,
        lead_minutes_effective=lead_minutes,
        price_band_start=price_band_start,
        price_band_full=price_band_full,
        price_median=price_median,
    )


def heat_curve_offset_c(
    compensated_outdoor_temp_c: float, raw_outdoor_temp_c: float, invert: bool
) -> int:
    """Convert the published spoof into a value for a pump's native heat
    curve offset ("värmekurvans förskjutning"), for pumps that expose one
    directly instead of needing a faked outdoor sensor.

    Sign convention differs by mechanism. This codebase's own offset is added
    to a spoofed OUTDOOR reading, where colder (more negative) means more heat
    — see learner.py's "Sign convention". Most pumps' native curve offset
    works the other way round, applied straight to the flow-temperature
    calculation: positive means warmer supply and more heat. `invert=False`
    (the common case) flips the sign to match that; `invert=True` is for the
    pumps that don't.

    Rounded to a whole number: this parameter is a small integer dial on
    every pump this has been checked against (e.g. NIBE's -10..+10), never a
    decimal one, and pushing a fraction risks the entity silently rejecting
    or truncating it rather than landing on the intended step.
    """
    delta_c = compensated_outdoor_temp_c - raw_outdoor_temp_c
    return round(delta_c if invert else -delta_c)
