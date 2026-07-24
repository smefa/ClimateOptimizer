"""Pure grey-box RC thermal model with online RLS parameter estimation.

Phase 2, SHADOW MODE ONLY. This module predicts and reports; it does not and
must not influence `compensated_outdoor_temp_c`. Like `heuristic.py`, it has
zero imports beyond the standard library and depends on no other module in
this package (not even `const.py`), on purpose: it is meant to be importable
and unit-testable in complete isolation, without Home Assistant installed.

Physical model (1R1C, single lumped thermal mass)
-------------------------------------------------
    C * dT_in/dt = (T_out - T_in) / R + gain * u + solar_coeff * s

where
    T_in  = indoor temperature (degC)
    T_out = *actual* outdoor temperature (degC), the envelope driver
    u     = the compensation delta the heuristic actually applied this cycle
            (compensated_outdoor_temp_c - raw_outdoor_temp_c). This is our
            only proxy for heat-pump action: the integration never commands
            heat directly and no true heat-input signal exists, so the
            effective "gain" that maps this delta to a thermal contribution
            is itself an unknown estimated jointly with the rest.
    s     = solar_effect in [0, 1] (same term the heuristic computes).

Discretised over a *variable* timestep dt (the coordinator interval is
user-configurable and cycles can be delayed or skipped, so dt is passed in as
actual elapsed time, in hours internally for good numerical conditioning):

    dT_in = theta_env * (T_out - T_in) * dt
          + theta_gain * u * dt
          + theta_solar * s * dt

with the lumped, *identifiable* parameters
    theta_env   = 1 / (R * C)   [1/h]   -> time constant tau = 1/theta_env [h]
    theta_gain  = gain / C              -> heat-pump effect per degC of delta
    theta_solar = solar_coeff / C       -> solar gain per unit solar_effect

Why these three, and why not R and C separately
------------------------------------------------
From indoor-temperature dynamics alone, without an absolute heat-input
measurement in physical units, R and C are NOT individually identifiable:
only the product tau = R*C (the decay/time constant) and the C-normalised
gains are. Trying to publish separate "R" and "C" from this data would be
reporting an arbitrary split, not an estimate. We therefore estimate the
identifiable lumped parameters and expose the physically meaningful
`time_constant_h` (tau), the heat-pump gain coefficient and the solar gain
coefficient. This is the deliberate "prefer the simpler, well-conditioned
formulation" call: each regressor is excited by a distinct physical driver
(envelope temperature difference, applied compensation delta, sunlight), so
the problem stays well-conditioned at typical HA cadences (e.g. 15 min,
noisy sensors), whereas adding a wind regressor by default (collinear with
the envelope term, both scaling with T_out - T_in) would make it fragile for
most houses.

Optional 4th term: wind (opt-in, per-installation)
----------------------------------------------------
For houses expected to be genuinely wind-sensitive (old, leaky, exposed —
the collinearity risk above is proportional to how *small* the true wind
effect is, and a leaky house's true effect is large enough to be
statistically distinguishable from the collinear envelope term), the caller
may enable a wind regressor via `RCModelConfig.enable_wind`:

    dT_in += theta_wind * (T_out - T_in) * (wind_speed / wind_reference_ms) * dt

This is an *interaction* term, not a plain additive `+ theta_wind * wind`,
because wind physically cannot cause heat loss with zero indoor/outdoor
temperature difference — both real wind-loss mechanisms (wind-driven
infiltration, exterior convective film) scale with the temperature gap, not
with wind speed alone. `theta_wind >= 0` is enforced as a hard physical
constraint: wind can only amplify heat exchange in whichever direction the
gradient already points, never reverse it. Wind speed is normalised by a
reference speed (default 5 m/s) before forming the interaction term
specifically to keep its magnitude comparable to the other regressors (which
are themselves deliberately `dt`-scaled for the same reason) — an
unnormalised raw-m/s interaction term can reach roughly 20-60x the envelope
regressor's typical range given realistic Nordic temperature/wind extremes,
which would reintroduce a conditioning problem via magnitude imbalance
instead of collinearity.

When disabled (the default), the estimator does not carry a wind dimension at
all, not a wind term silently zeroed — carrying a permanently-unexcited
dimension would still slowly inflate its share of the shared covariance-windup
budget every cycle purely from the forgetting factor, and over a long enough
run that would eventually trip the trace cap and rescale the *entire* matrix,
including the real parameters. Sizing the estimator itself (state
dimensionality tied to `enable_wind` at `initial_state()` time) avoids this
entirely.

Lazy heat-pump gain dimension (added only on real excitation)
-------------------------------------------------------------
The exact same covariance-windup argument applies to `theta_gain`. The gain
regressor is the *applied* compensation delta u, which is zero for as long as
the activation switch is off (learn mode publishes the raw outdoor
temperature, so nothing is applied) OR the heuristic's summer heating-cutoff
guardrail is engaged (compensated == raw, so the applied delta is zero even
with the switch on). During such an idle stretch — potentially the whole
warm half of the year — the gain dimension gets zero excitation, so nothing
opposes the forgetting factor inflating its variance every accepted cycle
(each of which IS still exciting envelope + solar from passive weather). Left
in the state vector, that idle dimension would trip the trace cap after
~1370 15-min cycles (~2 weeks) and then rescale the *entire* covariance
matrix — corrupting confidence in the envelope/solar parameters that are
being learned correctly the whole time.

So the estimator does NOT carry a gain dimension at cold start either. It
starts with only the dimensions actually excited by passive weather (env,
solar, and wind if enabled). The first time a genuinely nonzero applied delta
reaches an accepted update (see GAIN_EXCITATION_EPS — this is real excitation
reaching the model, NOT merely `is_active` being true), the state is expanded
by one dimension: `theta_gain` is appended LAST, preserving every existing
parameter value and its covariance block exactly (a block-embedding), with a
fresh wide prior variance on the new diagonal and zero cross-covariance to the
existing dimensions (honest — no evidence yet about how gain correlates). This
mirrors the wind on/off dimensionality split, but as a one-way, data-triggered
*expansion* rather than a config-triggered reset. Once added, gain stays added
permanently.

Known residual limitation (deliberately out of scope): if excitation stops
again for a long time after gain has been added (e.g. the switch goes back off
for another summer), the now-present gain dimension can slowly wind up again in
exactly the same way. Fixing that would require directional/selective
forgetting in the core RLS update law — a materially more invasive change,
left for future work.

Because gain is appended LAST (not at a fixed positional index), adding it never
disturbs the positions or covariance entries of env/solar/[wind]. The estimator
layout is therefore one of four shapes: [env, solar], [env, solar, wind],
[env, solar, gain], or [env, solar, wind, gain]. Note that two of these have
the same length (3): length alone cannot distinguish "wind, no gain" from
"gain, no wind", so both `enable_wind` and a persisted `has_gain` flag are
needed to disambiguate (see rc_store.py).

Estimation: Recursive Least Squares with a forgetting factor, plus guardrails
(covariance-windup capping + symmetrisation + PD safeguards, physically
bounded parameter clipping with counters, and two-stage outlier rejection).
See the individual functions for the concrete, justified thresholds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

MODEL_VERSION = "rc_rls_v1"

# --- RLS / model configuration (inlined; no const.py dependency) -------------

DEFAULT_FORGETTING_FACTOR = 0.99  # sane middle of the usual 0.98-0.995 band
DEFAULT_WIND_REFERENCE_MS = 5.0   # moderate/typical wind; keeps the wind
                                   # interaction term's magnitude comparable
                                   # to the envelope regressor (see module
                                   # docstring)

# Cold-start priors. A 30 h time constant is a plausible medium-mass house;
# the gain/solar/wind coefficients start at zero and are driven purely by
# data. There is no fixed positional index order any more: the estimator's
# layout is assembled by name (see _layout) so the gain dimension can be
# appended later without disturbing anything.
INIT_TAU_H = 30.0
INIT_THETA_ENV = 1.0 / INIT_TAU_H
INIT_THETA_GAIN = 0.0
INIT_THETA_SOLAR = 0.0
INIT_THETA_WIND = 0.0

# Per-parameter cold-start prior value, keyed by name.
_INIT_THETA = {
    "env": INIT_THETA_ENV,
    "solar": INIT_THETA_SOLAR,
    "wind": INIT_THETA_WIND,
    "gain": INIT_THETA_GAIN,
}
# Wide initial covariance so early estimates are not overconfident. Each theta
# is O(<=1) here, so a diagonal of 1.0 corresponds to a prior std of ~1, far
# larger than the priors themselves -> deliberately uncertain at cold start.
# Same value for every dimension (this IS the value the gain dimension gets when
# it is later added), keyed by name to match the by-name layout.
INIT_P_DIAG_VALUE = 1.0
_INIT_P = {
    "env": INIT_P_DIAG_VALUE,
    "solar": INIT_P_DIAG_VALUE,
    "wind": INIT_P_DIAG_VALUE,
    "gain": INIT_P_DIAG_VALUE,
}

# Physically sensible parameter bounds; clipping engages if the estimator
# tries to wander outside them. tau in [1 h, 500 h] -> theta_env in [1/500, 1].
TAU_MIN_H = 1.0
TAU_MAX_H = 500.0
THETA_ENV_MIN = 1.0 / TAU_MAX_H  # 0.002
THETA_ENV_MAX = 1.0 / TAU_MIN_H  # 1.0
THETA_GAIN_MIN = -5.0            # sign is brand-dependent; bound magnitude only
THETA_GAIN_MAX = 5.0
THETA_SOLAR_MIN = 0.0           # sunlight can only add heat
THETA_SOLAR_MAX = 5.0
THETA_WIND_MIN = 0.0            # wind can only amplify heat exchange, never
THETA_WIND_MAX = 0.5            # reverse it; upper bound is engineering
                                 # judgment (see module docstring), flagged
                                 # for revision once real data justifies it

# Physical bounds keyed by name (used via the estimator's current layout).
_THETA_BOUNDS = {
    "env": (THETA_ENV_MIN, THETA_ENV_MAX),
    "gain": (THETA_GAIN_MIN, THETA_GAIN_MAX),
    "solar": (THETA_SOLAR_MIN, THETA_SOLAR_MAX),
    "wind": (THETA_WIND_MIN, THETA_WIND_MAX),
}

# The heat-pump gain dimension is added only once a genuinely nonzero applied
# compensation delta (the gain regressor's driver) reaches an accepted update —
# NOT merely when the activation switch is on. The summer heating-cutoff can
# still zero the applied delta with the switch on, and learn mode zeros it with
# the switch off; both cases send exactly 0.0. This epsilon distinguishes "no
# excitation at all" (exactly 0.0) from any real applied delta; it is
# deliberately tiny so any real heat-pump action triggers the expansion.
GAIN_EXCITATION_EPS = 1e-9

# Covariance-windup guard: if excitation is poor the forgetting factor inflates
# P without bound. Cap its trace and rescale if exceeded.
P_TRACE_MAX = 1.0e6

# Maturity: number of accepted samples at which confidence saturates and the
# adaptive (residual-sigma) outlier gate is allowed to act.
WARMUP_SAMPLES = 20

# Outlier rejection thresholds (see step() for the documented two-stage rule).
OUTLIER_SIGMA = 4.0               # adaptive gate, in std-devs of recent residuals
ABS_MAX_INDOOR_STEP_C = 5.0       # a >5 degC one-cycle indoor swing is implausible
RESID_VAR_INIT = 0.01             # initial residual variance guess ((0.1 degC)^2)
RESID_VAR_EWMA_ALPHA = 0.05       # EWMA weight for tracking residual scale
RESID_VAR_FLOOR = 1.0e-4          # keep sigma away from zero ((0.01 degC)^2)

# dt sanity: reject and re-anchor on gaps (HA restart / long outage) or
# non-positive steps rather than corrupting the fit across a discontinuity.
MIN_DT_SECONDS = 1.0
MAX_DT_SECONDS = 6.0 * 3600.0


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _layout(enable_wind: bool, has_gain: bool) -> tuple[str, ...]:
    """Ordered parameter names for the given estimator configuration.

    env and solar are always present and keep fixed positions 0 and 1 so a
    later gain expansion never disturbs them; wind (if enabled) sits at 2; the
    heat-pump gain, when it has been added, is always appended LAST. Appending
    gain at the end (rather than a fixed index) is what makes the one-way
    expansion a clean block-embedding — every pre-existing parameter keeps its
    index and its covariance entries.
    """
    names = ["env", "solar"]
    if enable_wind:
        names.append("wind")
    if has_gain:
        names.append("gain")
    return tuple(names)


def _regressors(
    temp_gap: float,
    u: float,
    solar: float,
    wind_norm: float,
    dt_h: float,
    layout: tuple[str, ...],
) -> list[float]:
    """Build the regressor vector phi for the given layout, in layout order.

    `temp_gap` = T_out - T_in, `u` = applied compensation delta, `solar` in
    [0,1], `wind_norm` = wind_speed / wind_reference. Only the terms named in
    `layout` are emitted, so this serves every estimator shape without any
    positional assumptions.
    """
    values = {
        "env": temp_gap * dt_h,
        "solar": solar * dt_h,
        "wind": temp_gap * wind_norm * dt_h,
        "gain": u * dt_h,
    }
    return [values[name] for name in layout]


# --- tiny NxN linear algebra (stdlib only) -----------------------------------
# N is 2..4, taken from the length of the vectors/matrices passed in — never
# hardcoded, so the same code serves every estimator size without duplication.


def _matvec(matrix: list[list[float]], vec: list[float]) -> list[float]:
    n = len(vec)
    return [sum(matrix[i][j] * vec[j] for j in range(n)) for i in range(n)]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(a[i] * b[i] for i in range(len(a)))


def _trace(matrix: list[list[float]]) -> float:
    return sum(matrix[i][i] for i in range(len(matrix)))


def _all_finite(values) -> bool:
    return all(math.isfinite(v) for v in values)


# --- dataclasses -------------------------------------------------------------


@dataclass(frozen=True)
class RCModelConfig:
    """Estimator tuning. Defaults are production-sane; overridable in tests.

    `enable_wind` must match whatever `initial_state(enable_wind=...)` was
    constructed with — it determines whether the estimator carries a wind
    dimension, and `step()` assumes `state` and `config` agree. The
    coordinator sources both from the same config-entry option, and options
    changes trigger a full coordinator reload (fresh `initial_state()` call),
    so this invariant holds in practice without needing a runtime check here.
    """

    forgetting_factor: float = DEFAULT_FORGETTING_FACTOR
    outlier_sigma: float = OUTLIER_SIGMA
    abs_max_indoor_step_c: float = ABS_MAX_INDOOR_STEP_C
    warmup_samples: int = WARMUP_SAMPLES
    p_trace_max: float = P_TRACE_MAX
    enable_wind: bool = False
    wind_reference_ms: float = DEFAULT_WIND_REFERENCE_MS


@dataclass(frozen=True)
class RCModelState:
    """Persistent estimator state. Carried by the coordinator between cycles.

    Immutable: `step()` returns a fresh state, so there is no hidden mutable
    global state and the function is trivially unit-testable, exactly like
    `heuristic.compute()`. `theta`/`p_matrix` have length 2..4 depending on
    `enable_wind` and whether the heat-pump gain dimension has been added yet
    (`has_gain`); the ordered names for the current shape are
    `_layout(enable_wind, has_gain)`.

    `has_gain` records whether the lazily-added gain dimension is present. It
    is False at cold start and flips to True (permanently) the first cycle a
    genuinely nonzero applied compensation delta reaches an accepted update —
    see step(). It is persisted so the same-length ambiguity between
    [env, solar, wind] and [env, solar, gain] can be resolved on reload.
    """

    theta: tuple[float, ...]
    # Covariance P as a tuple-of-tuples (immutable); worked on as lists inside.
    p_matrix: tuple[tuple[float, ...], ...]
    has_gain: bool
    accepted_samples: int
    rejected_samples: int
    clip_events: int
    resid_var: float
    have_prev: bool
    prev_indoor_temp_c: float | None
    prev_outdoor_temp_c: float | None
    prev_u_c: float | None
    prev_solar: float | None
    prev_wind_speed_ms: float | None
    prev_predicted_next_indoor_c: float | None


@dataclass(frozen=True)
class RCModelInputs:
    """Live values for one cycle, gathered by the coordinator."""

    indoor_temp_c: float | None
    indoor_data_available: bool
    outdoor_temp_c: float          # *raw* outdoor temp (the envelope driver)
    compensation_delta_c: float    # u = compensated - raw applied this cycle
    solar_effect: float            # in [0, 1], as the heuristic computes it
    wind_speed_ms: float           # always a real value (coordinator defaults
                                    # to 0.0 when forecast wind is unavailable,
                                    # same soft-degrade convention as solar);
                                    # only used when RCModelConfig.enable_wind
    dt_seconds: float               # actual elapsed time since the last cycle


@dataclass(frozen=True)
class RCModelResult:
    """Explainable per-cycle output. Mirrors HeuristicResult's conventions."""

    predicted_next_indoor_temp_c: float | None
    prediction_error_c: float | None
    theta_env: float
    theta_gain: float               # 0.0 while `gain_modeled` is False (the
                                     # heat-pump gain dimension has not been
                                     # added yet — distinct from a learned 0.0)
    theta_solar: float
    theta_wind: float               # 0.0 (never estimated) unless wind enabled
    gain_modeled: bool              # has the gain dimension been added yet?
    time_constant_h: float
    accepted_samples: int
    rejected_samples: int
    clip_events: int
    cov_trace: float
    confidence: float              # 0..1 maturity indicator
    sample_accepted: bool          # was THIS cycle folded into the fit?
    clip_engaged: bool             # did any parameter hit a bound this cycle?
    reason: str
    model_version: str = MODEL_VERSION


def initial_state(enable_wind: bool = False) -> RCModelState:
    """Cold-start state: prior parameters + wide covariance, no history yet.

    `enable_wind` fixes whether the estimator carries a wind dimension for the
    lifetime of this state. The heat-pump gain dimension is NOT present at cold
    start (`has_gain=False`); it is added lazily on the first real excitation —
    see the module docstring for why this is done rather than always carrying a
    possibly-unexcited gain dimension.
    """
    layout = _layout(enable_wind, has_gain=False)
    theta = [_INIT_THETA[name] for name in layout]
    diag = [_INIT_P[name] for name in layout]
    n = len(theta)
    return RCModelState(
        theta=tuple(theta),
        p_matrix=tuple(
            tuple(diag[i] if i == j else 0.0 for j in range(n))
            for i in range(n)
        ),
        has_gain=False,
        accepted_samples=0,
        rejected_samples=0,
        clip_events=0,
        resid_var=RESID_VAR_INIT,
        have_prev=False,
        prev_indoor_temp_c=None,
        prev_outdoor_temp_c=None,
        prev_u_c=None,
        prev_solar=None,
        prev_wind_speed_ms=None,
        prev_predicted_next_indoor_c=None,
    )


def _add_gain_dimension(
    theta: list[float], p_matrix: list[list[float]]
) -> tuple[list[float], list[list[float]]]:
    """Block-embed a fresh heat-pump gain dimension, appended LAST.

    Existing parameters keep their values, and their entire covariance block is
    copied verbatim into the top-left of the new matrix; the new gain row/column
    gets the wide gain prior variance on the diagonal and zero cross-covariance
    to every existing dimension (honest: we have no evidence yet about how gain
    correlates with envelope/solar/wind). One-way and permanent — see step().
    """
    n = len(theta)
    new_theta = list(theta) + [_INIT_THETA["gain"]]
    new_p = [[p_matrix[i][j] for j in range(n)] + [0.0] for i in range(n)]
    new_p.append([0.0] * n + [_INIT_P["gain"]])
    return new_theta, new_p


def _confidence(accepted_samples: int, warmup: int) -> float:
    if warmup <= 0:
        return 1.0
    return _clamp(accepted_samples / warmup, 0.0, 1.0)


def _clip_theta(
    theta: list[float], layout: tuple[str, ...]
) -> tuple[list[float], bool]:
    """Clip parameters to physical bounds. Returns (clipped, any_clip_hit).

    Bounds are looked up per parameter name via the current `layout`, so this
    works for every estimator shape without positional assumptions.
    """
    n = len(theta)
    clipped = [_clamp(theta[i], *_THETA_BOUNDS[layout[i]]) for i in range(n)]
    hit = any(clipped[i] != theta[i] for i in range(n))
    return clipped, hit


def _rls_update(
    theta: list[float],
    p_matrix: list[list[float]],
    phi: list[float],
    y: float,
    forgetting: float,
    p_trace_max: float,
    layout: tuple[str, ...],
) -> tuple[list[float], list[list[float]], float, bool]:
    """One numerically-guarded RLS step, generic over the estimator size N
    (N = len(theta) = len(layout)).

    Returns (theta_new, p_new, a_priori_residual, clip_hit). Guards:
      * standard forgetting-factor RLS gain/covariance update,
      * symmetrise P each step (kills asymmetry drift over long runs),
      * reset P to the wide prior if it becomes non-finite or loses positive
        diagonal (positive-definiteness safeguard),
      * cap trace(P) to bound covariance windup under poor excitation,
      * clip theta to physical bounds and report if a bound was hit.
    """
    n = len(theta)
    p_phi = _matvec(p_matrix, phi)          # P * phi
    phi_p_phi = _dot(phi, p_phi)            # phi^T P phi (scalar, >= 0)
    denom = forgetting + phi_p_phi
    if denom <= 0 or not math.isfinite(denom):
        # Degenerate; skip the numeric update but keep parameters.
        return theta, p_matrix, 0.0, False

    gain = [p_phi[i] / denom for i in range(n)]
    residual = y - _dot(theta, phi)         # a priori error
    theta_new = [theta[i] + gain[i] * residual for i in range(n)]

    # P <- (P - gain * (P*phi)^T) / lambda
    p_new = [
        [(p_matrix[i][j] - gain[i] * p_phi[j]) / forgetting for j in range(n)]
        for i in range(n)
    ]
    # Symmetrise.
    for i in range(n):
        for j in range(i + 1, n):
            avg = 0.5 * (p_new[i][j] + p_new[j][i])
            p_new[i][j] = avg
            p_new[j][i] = avg

    # Positive-definiteness / finiteness safeguard.
    flat = [p_new[i][j] for i in range(n) for j in range(n)]
    diag_ok = all(p_new[i][i] > 0 for i in range(n))
    if not _all_finite(flat) or not _all_finite(theta_new) or not diag_ok:
        p_new = [
            [_INIT_P[layout[i]] if i == j else 0.0 for j in range(n)]
            for i in range(n)
        ]
        if not _all_finite(theta_new):
            theta_new = list(theta)

    # Covariance-windup cap.
    tr = _trace(p_new)
    if tr > p_trace_max and tr > 0:
        scale = p_trace_max / tr
        p_new = [[p_new[i][j] * scale for j in range(n)] for i in range(n)]

    theta_new, clip_hit = _clip_theta(theta_new, layout)
    return theta_new, p_new, residual, clip_hit


def _to_tuple_matrix(p_matrix: list[list[float]]):
    return tuple(tuple(row) for row in p_matrix)


def _result(
    state: RCModelState,
    config: RCModelConfig,
    predicted_next: float | None,
    prediction_error: float | None,
    sample_accepted: bool,
    clip_engaged: bool,
    reason: str,
) -> RCModelResult:
    layout = _layout(config.enable_wind, state.has_gain)
    idx = {name: i for i, name in enumerate(layout)}
    theta = state.theta
    theta_env = theta[idx["env"]]
    time_constant_h = 1.0 / theta_env if theta_env > 0 else float("inf")
    theta_solar = theta[idx["solar"]]
    theta_wind = theta[idx["wind"]] if "wind" in idx else 0.0
    gain_modeled = "gain" in idx
    theta_gain = theta[idx["gain"]] if gain_modeled else 0.0
    p_list = [list(row) for row in state.p_matrix]
    return RCModelResult(
        predicted_next_indoor_temp_c=predicted_next,
        prediction_error_c=prediction_error,
        theta_env=theta_env,
        theta_gain=theta_gain,
        theta_solar=theta_solar,
        theta_wind=theta_wind,
        gain_modeled=gain_modeled,
        time_constant_h=time_constant_h,
        accepted_samples=state.accepted_samples,
        rejected_samples=state.rejected_samples,
        clip_events=state.clip_events,
        cov_trace=_trace(p_list),
        confidence=_confidence(state.accepted_samples, config.warmup_samples),
        sample_accepted=sample_accepted,
        clip_engaged=clip_engaged,
        reason=reason,
    )


def step(
    state: RCModelState,
    inputs: RCModelInputs,
    config: RCModelConfig = RCModelConfig(),
) -> tuple[RCModelState, RCModelResult]:
    """Advance the shadow estimator by one cycle: (state, inputs) -> (state, result).

    Pure and side-effect free. The caller persists the returned state and feeds
    it back next cycle. `state` must have been created with
    `initial_state(enable_wind=config.enable_wind)` — see RCModelConfig's
    docstring. Outlier-rejection rule (two stages):
      1. Hard plausibility gate (always on): reject if any input is non-finite,
         if dt is out of [MIN_DT, MAX_DT] (a gap implies a discontinuity, e.g.
         HA restart -> re-anchor instead of fitting across it), or if the raw
         one-cycle indoor change exceeds abs_max_indoor_step_c (door/window
         opened or sensor glitch).
      2. Adaptive gate (only once mature, accepted >= warmup): reject if the
         a-priori residual exceeds outlier_sigma * std of recent residuals,
         where the residual variance is tracked as an EWMA over accepted
         samples only (so rejected outliers never inflate the scale).
    Rejected samples advance `prev` (so we do not keep re-measuring across a
    glitch) but never touch theta or P; they are counted and flagged.

    The heat-pump gain dimension is added lazily: on an accepted cycle whose
    gain regressor is genuinely excited (|prev applied delta| > excitation eps)
    while it is not yet present, the state is block-embedded up by one dimension
    before the RLS update (see _add_gain_dimension). See the module docstring.
    """
    indoor_ok = (
        inputs.indoor_data_available
        and inputs.indoor_temp_c is not None
        and math.isfinite(inputs.indoor_temp_c)
    )

    # Prediction error scoring (independent of whether we fit this cycle):
    # compare the previous cycle's stored prediction against the actual now.
    prediction_error: float | None = None
    if indoor_ok and state.prev_predicted_next_indoor_c is not None:
        prediction_error = inputs.indoor_temp_c - state.prev_predicted_next_indoor_c

    if not indoor_ok:
        # Cannot form a target; hold state, do not advance prev.
        return state, _result(
            state,
            config,
            state.prev_predicted_next_indoor_c,
            prediction_error,
            sample_accepted=False,
            clip_engaged=False,
            reason="Indoor sensor unavailable; RC shadow update skipped",
        )

    inputs_finite = _all_finite(
        (
            inputs.indoor_temp_c,
            inputs.outdoor_temp_c,
            inputs.compensation_delta_c,
            inputs.solar_effect,
            inputs.wind_speed_ms,
            inputs.dt_seconds,
        )
    )

    def anchored_state(predicted_next: float | None) -> RCModelState:
        """Re-anchor prev to current conditions, preserving learned params."""
        return replace(
            state,
            have_prev=True,
            prev_indoor_temp_c=inputs.indoor_temp_c,
            prev_outdoor_temp_c=inputs.outdoor_temp_c,
            prev_u_c=inputs.compensation_delta_c,
            prev_solar=inputs.solar_effect,
            prev_wind_speed_ms=inputs.wind_speed_ms,
            prev_predicted_next_indoor_c=predicted_next,
        )

    if not inputs_finite:
        new_state = anchored_state(None)
        return new_state, _result(
            new_state,
            config,
            None,
            prediction_error,
            sample_accepted=False,
            clip_engaged=False,
            reason="Non-finite input; RC shadow sample rejected, re-anchored",
        )

    if not state.have_prev:
        # First valid observation: initialise history, nothing to fit yet.
        new_state = anchored_state(None)
        return new_state, _result(
            new_state,
            config,
            None,
            prediction_error,
            sample_accepted=False,
            clip_engaged=False,
            reason="Cold start: RC shadow model initialised",
        )

    dt_seconds = inputs.dt_seconds
    if dt_seconds < MIN_DT_SECONDS or dt_seconds > MAX_DT_SECONDS:
        new_state = replace(
            anchored_state(None),
            rejected_samples=state.rejected_samples + 1,
        )
        return new_state, _result(
            new_state,
            config,
            None,
            prediction_error,
            sample_accepted=False,
            clip_engaged=False,
            reason=(
                f"dt {dt_seconds:.0f}s outside [{MIN_DT_SECONDS:.0f}, "
                f"{MAX_DT_SECONDS:.0f}]s (gap/discontinuity); rejected, re-anchored"
            ),
        )

    dt_h = dt_seconds / 3600.0
    y = inputs.indoor_temp_c - state.prev_indoor_temp_c  # realised dT over dt

    # Stage 1: hard plausibility gate on the raw indoor swing.
    if abs(y) > config.abs_max_indoor_step_c:
        new_state = replace(
            anchored_state(None),
            rejected_samples=state.rejected_samples + 1,
        )
        return new_state, _result(
            new_state,
            config,
            None,
            prediction_error,
            sample_accepted=False,
            clip_engaged=False,
            reason=(
                f"Implausible indoor step {y:+.1f} degC "
                f"(> {config.abs_max_indoor_step_c:.1f}); rejected as outlier"
            ),
        )

    # Regressors are driven by the *previous* cycle's conditions over dt, in the
    # current (pre-expansion) layout. The stage-2 gate below uses these; the
    # a-priori residual is unchanged by a pending gain expansion because the
    # fresh gain prior is exactly 0 (its regressor contributes 0 to the dot).
    wind_ref = config.wind_reference_ms if config.wind_reference_ms > 0 else (
        DEFAULT_WIND_REFERENCE_MS
    )
    prev_gap = state.prev_outdoor_temp_c - state.prev_indoor_temp_c
    prev_wind = (
        state.prev_wind_speed_ms if state.prev_wind_speed_ms is not None else 0.0
    )
    layout = _layout(config.enable_wind, state.has_gain)
    phi = _regressors(
        prev_gap, state.prev_u_c, state.prev_solar, prev_wind / wind_ref, dt_h, layout
    )
    theta_list = list(state.theta)
    a_priori_resid = y - _dot(theta_list, phi)

    # Stage 2: adaptive residual-sigma gate, only once mature.
    mature = state.accepted_samples >= config.warmup_samples
    sigma = math.sqrt(max(state.resid_var, RESID_VAR_FLOOR))
    if mature and abs(a_priori_resid) > config.outlier_sigma * sigma:
        new_state = replace(
            anchored_state(None),
            rejected_samples=state.rejected_samples + 1,
        )
        return new_state, _result(
            new_state,
            config,
            None,
            prediction_error,
            sample_accepted=False,
            clip_engaged=False,
            reason=(
                f"Residual {a_priori_resid:+.2f} degC exceeds "
                f"{config.outlier_sigma:.0f}sigma ({sigma:.2f}); rejected as outlier"
            ),
        )

    # --- accept: run the guarded RLS update -----------------------------------
    p_list = [list(row) for row in state.p_matrix]
    has_gain = state.has_gain

    # Lazy gain-dimension expansion: only now (on an accepted cycle) and only
    # when the gain regressor is genuinely excited. `is_active` alone is NOT
    # enough — the coordinator sends exactly 0.0 in learn mode and under the
    # summer heating-cutoff even with the switch on, so we key off the actual
    # applied delta reaching the model.
    if (
        not has_gain
        and math.isfinite(state.prev_u_c)
        and abs(state.prev_u_c) > GAIN_EXCITATION_EPS
    ):
        theta_list, p_list = _add_gain_dimension(theta_list, p_list)
        has_gain = True
        layout = _layout(config.enable_wind, has_gain)
        phi = _regressors(
            prev_gap, state.prev_u_c, state.prev_solar, prev_wind / wind_ref, dt_h, layout
        )

    theta_new, p_new, residual, clip_hit = _rls_update(
        theta_list,
        p_list,
        phi,
        y,
        config.forgetting_factor,
        config.p_trace_max,
        layout,
    )

    # Track residual scale (EWMA over accepted samples only).
    resid_var_new = (
        (1.0 - RESID_VAR_EWMA_ALPHA) * state.resid_var
        + RESID_VAR_EWMA_ALPHA * residual * residual
    )
    resid_var_new = max(resid_var_new, RESID_VAR_FLOOR)

    # Predict next indoor temp from current conditions and the updated params,
    # using this cycle's dt as the best available estimate of the next interval.
    gap_now = inputs.outdoor_temp_c - inputs.indoor_temp_c
    phi_now = _regressors(
        gap_now,
        inputs.compensation_delta_c,
        inputs.solar_effect,
        inputs.wind_speed_ms / wind_ref,
        dt_h,
        layout,
    )
    predicted_next = inputs.indoor_temp_c + _dot(theta_new, phi_now)

    new_state = RCModelState(
        theta=tuple(theta_new),
        p_matrix=_to_tuple_matrix(p_new),
        has_gain=has_gain,
        accepted_samples=state.accepted_samples + 1,
        rejected_samples=state.rejected_samples,
        clip_events=state.clip_events + (1 if clip_hit else 0),
        resid_var=resid_var_new,
        have_prev=True,
        prev_indoor_temp_c=inputs.indoor_temp_c,
        prev_outdoor_temp_c=inputs.outdoor_temp_c,
        prev_u_c=inputs.compensation_delta_c,
        prev_solar=inputs.solar_effect,
        prev_wind_speed_ms=inputs.wind_speed_ms,
        prev_predicted_next_indoor_c=predicted_next,
    )

    idx = {name: i for i, name in enumerate(layout)}
    tau = 1.0 / theta_new[idx["env"]] if theta_new[idx["env"]] > 0 else float("inf")
    gain_txt = f"{theta_new[idx['gain']]:+.3f}" if "gain" in idx else "not-yet-modeled"
    reason = (
        f"RC RLS: tau={tau:.1f}h, gain={gain_txt}, "
        f"solar={theta_new[idx['solar']]:.3f}"
    )
    if config.enable_wind:
        reason += f", wind={theta_new[idx['wind']]:+.3f}"
    if has_gain and not state.has_gain:
        reason += "; gain dimension added (first heat-pump excitation)"
    reason += (
        f"; accepted (resid {residual:+.2f} degC); "
        f"{new_state.accepted_samples} accepted / {new_state.rejected_samples} "
        f"rejected; confidence "
        f"{_confidence(new_state.accepted_samples, config.warmup_samples) * 100:.0f}%"
    )
    if clip_hit:
        reason += "; parameter clip engaged"
    if prediction_error is not None:
        reason += f"; prev pred err {prediction_error:+.2f} degC"

    return new_state, _result(
        new_state,
        config,
        predicted_next,
        prediction_error,
        sample_accepted=True,
        clip_engaged=clip_hit,
        reason=reason,
    )
