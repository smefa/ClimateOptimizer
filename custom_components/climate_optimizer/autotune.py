"""Derive the heuristic's coefficients from the learned RC model.

Like `heuristic.py`, `rc_model.py` and `mpc.py`, this module imports nothing
beyond the standard library and depends on no other module in this package
(not even `const.py`), on purpose: it is meant to be importable and
unit-testable in complete isolation, without Home Assistant installed.

The problem this solves
-----------------------
`k_indoor`, `k_wind`, `k_sun` and `k_price` are configured in units of
"compensated-outdoor degC per unit of input". That unit is house-specific:
the same 1.5 that damps one house oscillates another. Worse, it is also
*outdoor-temperature*-specific, because a heat pump's weather curve is not
linear — 1 degC of spoofing buys a different amount of heat at -15 degC than at
+5 degC (the curve steepens in cold, while the compressor simultaneously
approaches its capacity limit). A single fixed coefficient cannot be right
across a season.

Both problems have the same root: the coefficients are plant parameters
written down by hand. The RC model already estimates the plant. So derive
them instead of asking.

The derivation
--------------
Invert the RC model's own control channel. From rc_model.py:

    dT_in/dt = theta_env * (T_out - T_in)
             + theta_gain * u
             + theta_solar * s
             [+ theta_wind * (T_out - T_in) * (wind / wind_ref)]

To make the controller contribute a desired indoor heating rate, solve for u:

    theta_gain * u = desired_rate   ->   u = desired_rate / theta_gain

Every coefficient falls out of that one inversion:

  k_indoor  For an indoor error e = target - indoor, we want e to decay with
            closed-loop time constant tau_cl, i.e. contribute e / tau_cl:
                u = e / (tau_cl * theta_gain)   ->   k_indoor = 1 / (tau_cl * |theta_gain|)

  k_sun     Sunlight already adds theta_solar * s. Back the pump off by exactly
            that much:
                u = -theta_solar * s / theta_gain   ->   k_sun = theta_solar / |theta_gain|

  k_wind    Wind adds a loss of theta_wind * (T_out - T_in) * (w / wind_ref).
            Offsetting it gives
                k_wind = theta_wind * |T_out - T_in| / (wind_ref * |theta_gain|)
            Note this depends on the indoor/outdoor gap, because wind loss
            physically scales with the gap (see rc_model.py's argument for why
            its wind term is an *interaction*, not a plain additive term).
            heuristic.py's hand-tuned form is `-k_wind * wind_speed` with no gap
            factor, which is dimensionally wrong; deriving the coefficient from
            the live gap every cycle fixes that inconsistency as a side effect.

  k_price   A price-driven target sag is just an indoor error the controller is
            deliberately choosing to accept, so the same law applies, scaled by
            PRICE_AUTHORITY_RATIO (see that constant).

Why non-linearity is handled for free: every coefficient above is divided by
the *currently estimated* |theta_gain|. When the pump's curve steepens in cold
and theta_gain grows, the derived coefficients shrink by exactly the same
factor; when the compressor saturates and theta_gain collapses, they grow and
the controller correctly runs toward full authority. RLS's forgetting factor
(rc_model.DEFAULT_FORGETTING_FACTOR, ~1 day of memory) is already tracking the
local slope of the curve; this module is what finally *uses* it.

Choosing tau_cl without asking
------------------------------
tau_cl is the one quantity in the derivation that isn't already estimated. It
is nonetheless not a preference: nobody wants slower correction at equal cost,
and the comfort/cost tradeoff is expressed separately by the price tier. The
right value is "as fast as the plant allows without ringing", which is a plant
property:

    tau_cl = clamp(tau_open / SPEEDUP, emitter_floor, TAU_CL_MAX_H)

with tau_open = 1 / theta_env, already the best-estimated parameter in the
model (passive weather excites it every single cycle, which is why it never
needed rc_model.py's lazy-dimension machinery). SPEEDUP is dimensionless, so
unlike k_indoor it is legitimately a hardcoded design constant rather than a
house property in disguise.

The floor is where the unmodelled dead time is acknowledged. The 1R1C model has
no transport delay, so `tau_open / SPEEDUP` alone would happily propose a
closed-loop response faster than the emitters can physically deliver. On a
heavy house the ratio is far above the lag and the floor never binds; on a light
one it does. That is what the heating-type question buys.

What is deliberately NOT derived
--------------------------------
The COP half of the cold taper. Heat bought back at -15 degC genuinely costs
more per kWh than the model's proportional-to-heat cost assumes, but the only
available power signal is documented in const.py as contaminated by hot-water
production and deliberately excluded from control. Rather than invent a COP
curve, the derived taper models recovery *feasibility* only (see
`derived_cold_taper`) and the residual is left unmodelled and stated.

Safety: the ramp, not the prior
-------------------------------
Every formula here divides by theta_gain, so a bad theta_gain does not merely
detune the loop — near zero it produces an enormous u. Three hard gates and one
soft ramp guard that:

  * `gain_modeled` must be True (rc_model.py only adds the gain dimension after
    real heat-pump excitation; before that theta_gain is a not-yet-modeled 0.0,
    not a learned one),
  * |theta_gain| must clear MIN_GAIN_MAGNITUDE,
  * no RC parameter may be pinned at a clip bound,
  * and even then the derived values are blended in proportionally to
    accumulated evidence (see `blend_weight`), starting at the configured manual
    values and stepping toward the derived ones over WARMUP_SAMPLES cycles.

The ramp lives here rather than in rc_model.py's cold-start prior on purpose.
theta_env is so well excited that its prior is washed out within hours, so
starting it low and stepping it up would change almost nothing; and it would
step in the *unsafe* direction, since a shorter assumed tau_open yields a
shorter tau_cl and therefore a LARGER k_indoor. Ramping authority instead is
both effective and monotonically safe: at zero evidence the controller is
exactly the hand-tuned one it replaces.
"""

from __future__ import annotations

from dataclasses import dataclass

MODEL_VERSION = "autotune_v1"

# --- Tuning mode -------------------------------------------------------------
# Which set of coefficients actually drives the controller. The derivation runs
# and is published in BOTH modes — that is what makes a side-by-side comparison
# possible before anyone commits — so the mode selects only which numbers
# heuristic.compute() is handed.
TUNING_MODE_MANUAL = "manual"
TUNING_MODE_AUTO = "auto"
TUNING_MODES = (TUNING_MODE_MANUAL, TUNING_MODE_AUTO)


def resolve_tuning_mode(name: str | None) -> str:
    """Normalise a tuning-mode name, defaulting to Manual on anything
    unrecognized — a bad config value must degrade to the hand-tuned
    coefficients, never to the derived ones."""
    candidate = (name or "").strip().lower()
    return candidate if candidate in TUNING_MODES else TUNING_MODE_MANUAL


# --- Design constants (all dimensionless or absolute-physical) ---------------

# Closed-loop speedup: tau_cl = tau_open / CLOSED_LOOP_SPEEDUP. Dimensionless,
# so it travels across houses in a way k_indoor never could. 3.0 is the
# conventional "noticeably faster than open loop, comfortably short of the
# aggressiveness where unmodelled lag starts to ring" choice.
CLOSED_LOOP_SPEEDUP = 3.0

# Absolute bounds on the derived closed-loop time constant, applied after the
# emitter floor. The upper bound is "slower than this isn't controlling, it's
# just watching"; the lower is a last-resort guard for a degenerate theta_env.
TAU_CL_MIN_H = 1.0
TAU_CL_MAX_H = 24.0

# Heating-system types. These exist only to set the dead-time floor on tau_cl:
# the RC model has no transport delay term, so this is the single place where
# emitter/water-loop lag enters the derivation. Underfloor heating buries its
# emitter in a slab that takes hours to respond; radiators are much closer to
# the water temperature.
HEATING_TYPE_UNDERFLOOR = "underfloor"
HEATING_TYPE_RADIATORS = "radiators"

EMITTER_TAU_FLOOR_H: dict[str, float] = {
    HEATING_TYPE_UNDERFLOOR: 4.0,
    HEATING_TYPE_RADIATORS: 2.0,
}
DEFAULT_HEATING_TYPE = HEATING_TYPE_RADIATORS

# Price braking is allowed to be more aggressive than error correction. This is
# not arbitrary: braking is bounded below by the comfort floor and is
# self-limiting (heuristic.py resumes heating as indoor approaches
# target - max_sag_c), whereas over-aggressive heating overshoots with nothing
# to catch it. The ratio preserves the intent of the hand-tuned defaults, whose
# k_price / k_indoor was 5.0 / 1.5.
PRICE_AUTHORITY_RATIO = 3.3

# Below this |theta_gain| the inversion is numerically meaningless (1/g blows
# up) and, more importantly, physically means "we cannot tell how the pump
# responds". Matches mpc.DEFAULT_MIN_GAIN_MAGNITUDE in spirit and magnitude.
MIN_GAIN_MAGNITUDE = 0.01

# Accepted RC samples at which the derived values are trusted at full weight.
# Deliberately far longer than rc_model.WARMUP_SAMPLES (20, ~5 hours), which
# governs when the estimator's own outlier gate may act — a sensible threshold
# for "is this residual an outlier", far too short for "should this model be
# allowed to retune the controller". 480 samples is ~5 days at the default
# 15-minute cadence: long enough to have seen several diurnal cycles.
WARMUP_SAMPLES = 480

# Derived-value clamps. These mirror the config-flow ranges for the manual
# equivalents, so Auto mode can never propose a coefficient the user could not
# have typed in Manual mode. A derived value hitting one of these is reported
# via `clamped` rather than silently accepted.
K_INDOOR_MIN, K_INDOOR_MAX = 0.0, 10.0
K_WIND_MIN, K_WIND_MAX = 0.0, 3.0
K_SUN_MIN, K_SUN_MAX = 0.0, 10.0
K_PRICE_MIN, K_PRICE_MAX = 0.0, 15.0
COLD_TAPER_MIN, COLD_TAPER_MAX = 0.0, 1.0

# Wind normalisation reference; must match the value the RC estimator was fit
# with, since theta_wind is defined relative to it (see rc_model.py).
DEFAULT_WIND_REFERENCE_MS = 5.0


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def emitter_tau_floor_h(heating_type: str | None) -> float:
    """Dead-time floor on tau_cl for a heating-system type, defaulting to
    radiators on anything unrecognized (the faster of the two, but still a real
    floor — a bad config value degrades to the common case rather than to no
    guard at all)."""
    return EMITTER_TAU_FLOOR_H.get(
        (heating_type or "").strip().lower(),
        EMITTER_TAU_FLOOR_H[DEFAULT_HEATING_TYPE],
    )


def derived_tau_cl_h(theta_env: float, heating_type: str | None) -> float:
    """Target closed-loop time constant, in hours.

    `theta_env <= 0` would imply a house that never loses heat; fall back to the
    emitter floor rather than dividing by zero or proposing an infinite tau.
    """
    floor = max(emitter_tau_floor_h(heating_type), TAU_CL_MIN_H)
    if theta_env <= 0.0:
        return floor
    tau_open_h = 1.0 / theta_env
    return _clamp(tau_open_h / CLOSED_LOOP_SPEEDUP, floor, TAU_CL_MAX_H)


def derived_cold_taper(
    theta_env: float,
    theta_gain: float,
    tau_cl_h: float,
    outdoor_c: float,
    indoor_c: float,
    max_heating_delta_c: float,
    tier_max_sag_c: float,
) -> float:
    """Price-braking authority scaled by whether the sag is actually
    recoverable, replacing the hand-drawn outdoor-temperature taper.

    The question the original three-knob taper was approximating is not "is it
    cold" but "can I recover this sag before I need to be back at target". The
    RC model answers that directly. At full heating authority the net recovery
    rate is

        rate = |theta_gain| * u_max + theta_env * (T_out - T_in)   [degC/h]

    where the second term is the envelope working against us, and is more
    negative the colder it is outside. So the cold taper falls out of the
    physics rather than being drawn by hand: in deep cold the loss term eats
    the recovery rate, less sag is recoverable, and authority tapers — with a
    shape derived from this specific house.

    The recovery window is tau_cl: the same "how fast should errors close"
    horizon the rest of the derivation targets, so no new constant is needed.

    Returns a factor in [0, 1] directly comparable to the manual
    `heuristic.cold_taper_factor` output. A non-positive `tier_max_sag_c` means
    the tier does not sag at all, so the taper is irrelevant -> 1.0.
    """
    if tier_max_sag_c <= 0.0:
        return 1.0
    recovery_rate = abs(theta_gain) * max(0.0, max_heating_delta_c) + theta_env * (
        outdoor_c - indoor_c
    )
    if recovery_rate <= 0.0:
        # Cannot recover at all at full authority: do not spend comfort we have
        # no way to buy back.
        return COLD_TAPER_MIN
    recoverable_sag_c = recovery_rate * tau_cl_h
    return _clamp(recoverable_sag_c / tier_max_sag_c, COLD_TAPER_MIN, COLD_TAPER_MAX)


@dataclass(frozen=True)
class AutotuneInputs:
    """Everything the derivation needs, gathered by the coordinator.

    The `theta_*` values and the three trust flags come straight off the latest
    `rc_model.RCModelResult`; the manual `k_*` values are the configured
    fallbacks that the blend starts from and returns to.
    """

    # --- learned plant, from RCModelResult ---
    theta_env: float
    theta_gain: float
    theta_solar: float
    theta_wind: float
    gain_modeled: bool
    params_pinned: bool
    accepted_samples: int
    # --- live conditions (k_wind is gap-dependent; the taper is gap- and
    # outdoor-dependent) ---
    indoor_temp_c: float | None
    outdoor_temp_c: float
    # --- configuration ---
    heating_type: str
    wind_reference_ms: float
    max_heating_delta_c: float
    tier_max_sag_c: float
    enable_solar: bool
    enable_wind: bool
    # --- manual fallbacks (what Manual mode would use) ---
    manual_k_indoor: float
    manual_k_wind: float
    manual_k_sun: float
    manual_k_price: float
    manual_cold_taper_factor: float


@dataclass(frozen=True)
class AutotuneResult:
    """Derived coefficients, the blended values actually usable for control,
    and enough explanation to answer "why is it not using them yet".

    `*_derived` are the pure derivation. `*_effective` are what a caller in Auto
    mode should actually apply: `manual + blend_weight * (derived - manual)`.
    In Manual mode a caller ignores the effective values entirely and uses its
    own configured ones — but the derived values are still computed and
    published, which is what makes a season of side-by-side comparison possible
    before anyone flips the switch.
    """

    usable: bool
    blend_weight: float
    tau_open_h: float
    tau_cl_h: float
    k_indoor_derived: float
    k_wind_derived: float
    k_sun_derived: float
    k_price_derived: float
    cold_taper_derived: float
    k_indoor_effective: float
    k_wind_effective: float
    k_sun_effective: float
    k_price_effective: float
    cold_taper_effective: float
    clamped: bool
    reason: str
    model_version: str = MODEL_VERSION


def log_fields(
    result: AutotuneResult | None,
    tuning_mode: str,
    manual: tuple[float, float, float, float],
) -> dict:
    """Flatten a derivation into JSONL-log fields (see data_logger.py).

    Kept here rather than in coordinator.py so it is covered by CI: the pure
    modules are the only ones the test job can import (it never installs
    `homeassistant`), and this is the record that the whole "watch
    derived-vs-manual for a season, then decide" workflow depends on being
    correct.

    Three sets of values are logged deliberately, not one:
      * `*_manual`    — what Manual mode would have used,
      * `*_derived`   — what the model proposes on its own,
      * `*_effective` — what was ACTUALLY in force this cycle.
    Only the third is recoverable from nothing else. Without it an offline
    replay cannot tell what the controller was doing, because in Auto mode the
    effective values move every cycle and are not inferable from the stored
    config. The first two make the comparison possible without re-deriving.

    `reason` is logged only when the derivation is NOT usable — that is the
    diagnostic case, and it is low-cardinality, whereas logging the full reason
    string every cycle would dominate the file for a value that is
    reconstructible from the numeric fields.
    """
    manual_k_indoor, manual_k_wind, manual_k_sun, manual_k_price = manual
    fields: dict = {
        "tuning_mode": tuning_mode,
        "k_indoor_manual": manual_k_indoor,
        "k_wind_manual": manual_k_wind,
        "k_sun_manual": manual_k_sun,
        "k_price_manual": manual_k_price,
    }
    if result is None:
        fields["autotune_usable"] = False
        fields["autotune_block_reason"] = "no RC model result yet"
        return fields
    fields.update(
        {
            "autotune_usable": result.usable,
            "autotune_blend_weight": result.blend_weight,
            "autotune_tau_open_h": result.tau_open_h,
            "autotune_tau_cl_h": result.tau_cl_h,
            "autotune_clamped": result.clamped,
            "k_indoor_derived": result.k_indoor_derived,
            "k_wind_derived": result.k_wind_derived,
            "k_sun_derived": result.k_sun_derived,
            "k_price_derived": result.k_price_derived,
            "cold_taper_derived": result.cold_taper_derived,
            "k_indoor_effective": result.k_indoor_effective,
            "k_wind_effective": result.k_wind_effective,
            "k_sun_effective": result.k_sun_effective,
            "k_price_effective": result.k_price_effective,
            "cold_taper_effective": result.cold_taper_effective,
        }
    )
    if not result.usable:
        fields["autotune_block_reason"] = result.reason
    return fields


def _blocking_reason(inputs: AutotuneInputs) -> str | None:
    """Why the derivation cannot be trusted at all this cycle, or None."""
    if not inputs.gain_modeled:
        return (
            "heat pump not yet excited — no compensation delta has been applied, "
            "so the model has no gain to invert"
        )
    if abs(inputs.theta_gain) < MIN_GAIN_MAGNITUDE:
        return (
            f"|theta_gain|={abs(inputs.theta_gain):.4f} below "
            f"{MIN_GAIN_MAGNITUDE:.4f} — cannot tell how the pump responds"
        )
    if inputs.params_pinned:
        return "an RC parameter is pinned at a clip bound — model not trustworthy"
    if inputs.theta_env <= 0.0:
        return f"theta_env={inputs.theta_env:.4f} non-physical (house never loses heat)"
    return None


def derive(inputs: AutotuneInputs) -> AutotuneResult:
    """Derive controller coefficients from the learned plant.

    Pure and side-effect free, like `heuristic.compute` and `rc_model.step`.
    Always returns a full result: when the model cannot be trusted the derived
    values fall back to the manual ones and `usable` is False with `reason`
    saying why, so the "why isn't Auto doing anything" question is answerable
    from the published attributes alone.
    """
    blocked = _blocking_reason(inputs)
    tau_open_h = 1.0 / inputs.theta_env if inputs.theta_env > 0.0 else float("inf")
    tau_cl_h = derived_tau_cl_h(inputs.theta_env, inputs.heating_type)

    if blocked is not None:
        # Nothing to derive from; every "derived" value mirrors manual so a
        # caller that blindly reads the effective values still behaves exactly
        # like Manual mode.
        return AutotuneResult(
            usable=False,
            blend_weight=0.0,
            tau_open_h=tau_open_h,
            tau_cl_h=tau_cl_h,
            k_indoor_derived=inputs.manual_k_indoor,
            k_wind_derived=inputs.manual_k_wind,
            k_sun_derived=inputs.manual_k_sun,
            k_price_derived=inputs.manual_k_price,
            cold_taper_derived=inputs.manual_cold_taper_factor,
            k_indoor_effective=inputs.manual_k_indoor,
            k_wind_effective=inputs.manual_k_wind,
            k_sun_effective=inputs.manual_k_sun,
            k_price_effective=inputs.manual_k_price,
            cold_taper_effective=inputs.manual_cold_taper_factor,
            clamped=False,
            reason=f"Auto-tune not usable: {blocked}; using manual coefficients",
        )

    gain = abs(inputs.theta_gain)

    # --- the derivation proper -------------------------------------------
    raw_k_indoor = 1.0 / (tau_cl_h * gain)
    raw_k_price = PRICE_AUTHORITY_RATIO / (tau_cl_h * gain)
    raw_k_sun = (inputs.theta_solar / gain) if inputs.enable_solar else 0.0

    if inputs.enable_wind:
        # Wind loss scales with the indoor/outdoor gap, so the derived
        # coefficient does too (see the module docstring). Without an indoor
        # reading there is no gap to scale by, so the wind term stands down for
        # this cycle rather than guessing one.
        wind_ref = (
            inputs.wind_reference_ms
            if inputs.wind_reference_ms > 0
            else DEFAULT_WIND_REFERENCE_MS
        )
        if inputs.indoor_temp_c is None:
            raw_k_wind = 0.0
        else:
            gap_c = abs(inputs.outdoor_temp_c - inputs.indoor_temp_c)
            raw_k_wind = inputs.theta_wind * gap_c / (wind_ref * gain)
    else:
        raw_k_wind = 0.0

    if inputs.indoor_temp_c is None:
        # The recovery-feasibility taper needs the current gap; without it, fall
        # back to the manually configured taper for this cycle.
        raw_cold_taper = inputs.manual_cold_taper_factor
    else:
        raw_cold_taper = derived_cold_taper(
            theta_env=inputs.theta_env,
            theta_gain=inputs.theta_gain,
            tau_cl_h=tau_cl_h,
            outdoor_c=inputs.outdoor_temp_c,
            indoor_c=inputs.indoor_temp_c,
            max_heating_delta_c=inputs.max_heating_delta_c,
            tier_max_sag_c=inputs.tier_max_sag_c,
        )

    k_indoor_derived = _clamp(raw_k_indoor, K_INDOOR_MIN, K_INDOOR_MAX)
    k_wind_derived = _clamp(raw_k_wind, K_WIND_MIN, K_WIND_MAX)
    k_sun_derived = _clamp(raw_k_sun, K_SUN_MIN, K_SUN_MAX)
    k_price_derived = _clamp(raw_k_price, K_PRICE_MIN, K_PRICE_MAX)
    cold_taper_derived = _clamp(raw_cold_taper, COLD_TAPER_MIN, COLD_TAPER_MAX)
    clamped = any(
        derived != raw
        for derived, raw in (
            (k_indoor_derived, raw_k_indoor),
            (k_wind_derived, raw_k_wind),
            (k_sun_derived, raw_k_sun),
            (k_price_derived, raw_k_price),
            (cold_taper_derived, raw_cold_taper),
        )
    )

    # --- evidence ramp ----------------------------------------------------
    weight = _clamp(inputs.accepted_samples / float(WARMUP_SAMPLES), 0.0, 1.0)

    def blend(manual: float, derived: float) -> float:
        return manual + weight * (derived - manual)

    reason = (
        f"Auto-tune from tau_open={tau_open_h:.1f}h, |gain|={gain:.3f} "
        f"-> tau_cl={tau_cl_h:.1f}h ({inputs.heating_type}); "
        f"k_indoor {inputs.manual_k_indoor:.2f}->{k_indoor_derived:.2f}, "
        f"k_price {inputs.manual_k_price:.2f}->{k_price_derived:.2f}, "
        f"k_sun {inputs.manual_k_sun:.2f}->{k_sun_derived:.2f}, "
        f"k_wind {inputs.manual_k_wind:.2f}->{k_wind_derived:.2f}, "
        f"taper {inputs.manual_cold_taper_factor:.2f}->{cold_taper_derived:.2f}; "
        f"blend {weight * 100:.0f}% "
        f"({inputs.accepted_samples}/{WARMUP_SAMPLES} samples)"
    )
    if clamped:
        reason += "; a derived value hit its clamp"

    return AutotuneResult(
        usable=True,
        blend_weight=weight,
        tau_open_h=tau_open_h,
        tau_cl_h=tau_cl_h,
        k_indoor_derived=k_indoor_derived,
        k_wind_derived=k_wind_derived,
        k_sun_derived=k_sun_derived,
        k_price_derived=k_price_derived,
        cold_taper_derived=cold_taper_derived,
        k_indoor_effective=blend(inputs.manual_k_indoor, k_indoor_derived),
        k_wind_effective=blend(inputs.manual_k_wind, k_wind_derived),
        k_sun_effective=blend(inputs.manual_k_sun, k_sun_derived),
        k_price_effective=blend(inputs.manual_k_price, k_price_derived),
        cold_taper_effective=blend(
            inputs.manual_cold_taper_factor, cold_taper_derived
        ),
        clamped=clamped,
        reason=reason,
    )
