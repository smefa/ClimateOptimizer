"""Pure Model Predictive Control (MPC) planner for ClimateOptimizer.

Phase 3, SHADOW / ADVISORY MODE ONLY. Like `heuristic.py` and `rc_model.py`,
this module has zero imports beyond the standard library and depends on no
other module in this package (not even `const.py`), on purpose: it is meant to
be importable and unit-testable in complete isolation, without Home Assistant
installed. It predicts and recommends; it does not and must not influence
`compensated_outdoor_temp_c`. The heuristic pipeline gated by
`switch.<name>_active` remains the only thing that actually controls anything.

What this does
--------------
Given the RC model's currently-learned physical parameters (envelope, heat-pump
gain, solar, optional wind), forecast arrays over a multi-hour horizon (price,
outdoor temperature, and — only when enabled — wind), and the current indoor
temperature, it plans a cost-minimising sequence of compensation deltas that
keeps the predicted indoor temperature inside the hard comfort band
[comfort_min, comfort_max] at every step, and returns the *first-step*
recommendation plus a fully explainable result.

It is designed for **receding-horizon** use: the coordinator re-solves this
every cycle with the latest forecasts and only ever applies (would apply) the
first step, discarding the rest of the plan. Nothing here persists state
between calls — each `plan()` is a pure function of its inputs, exactly like
`heuristic.compute()`.

The model this plans against (matching rc_model.py)
---------------------------------------------------
The RC model identifies the discretised 1R1C dynamics, per timestep dt (hours):

    T_next = T
           + theta_env  * (T_out - T)                       * dt   # envelope
           + theta_gain * u                                 * dt   # heat pump
           + theta_solar * s                                * dt   # solar gain
           [+ theta_wind * (T_out - T) * (wind/wind_ref)    * dt]  # opt. wind

where u is the *compensation delta* (compensated_outdoor - raw_outdoor) — the
only proxy the whole integration has for heat-pump action. `theta_gain` is
typically NEGATIVE (a colder reading, u < 0, makes the pump deliver more heat,
raising indoor temperature: theta_gain * u > 0). We plan `u` directly, so the
recommendation is in the same units the heuristic already emits and could one
day publish.

Heating-only control (never cooling)
------------------------------------
In the RC model's identified frame, u = 0 corresponds to *no heat-pump
contribution at all* (the pump's own baseline weather curve is not separately
modelled — see rc_model.py's limitations). u < 0 delivers heat; u > 0 would
imply q = theta_gain*u < 0, i.e. actively pulling heat out, which this project
deliberately never does (mirroring the heuristic's summer-cutoff "only ever
stop heating, never start cooling" philosophy). We therefore restrict the
control to u in [-max_heating_delta_c, 0]: choose how much heat to add, from
none (coast, and let the envelope cool the house) up to full authority.

A consequence worth stating plainly: because u = 0 means "no pump heat" in this
frame, the *absolute* predicted trajectory is in the model's own reference, not
real wall-thermometer degrees — a coasting house is predicted to drift toward
outdoor temperature. What is meaningful, and what MPC is actually for, is the
*relative* decision: given prices and the house's thermal storage (its comfort
band acted as a battery), when to spend heat. This is one more reason the
output is advisory/shadow-only for now.

Cost function (relative, proxy units)
-------------------------------------
Electrical energy is proportional to heat delivered, which in the model's frame
is q = theta_gain * u (degC/h of indoor rise attributable to the pump). Only
heating is paid for (q > 0); coasting is free. The per-step cost is

    stage_cost = price[t] * max(0, theta_gain * u) * dt

The absolute scale (the unknown thermal capacitance C) is a positive constant
that factors out of every comparison, so it does not affect which plan is
optimal. Reported costs are therefore in **relative proxy units** (price ×
degC), useful for ranking plans and reporting savings-vs-baseline, NOT a real
currency figure. This is stated on the sensors too.

Solver: dynamic programming over a discretised indoor-temperature state
-----------------------------------------------------------------------
Chosen (per the agreed design) over LP/QP: no scipy dependency, lightweight,
and inherently explainable — you can inspect the value table and see exactly
which constraint binds. The indoor-temperature state is discretised across the
comfort band; a backward value-iteration pass computes the cost-to-go at every
(step, temperature) node using linear interpolation of the next stage's value
at the (continuous) predicted next temperature; a forward pass from the actual
current temperature then reads off the optimal control sequence.

Comfort bounds are enforced as a large penalty on any transition leaving
[comfort_min, comfort_max]. The penalty dominates any conceivable price cost, so
feasible plans never violate comfort; but keeping it finite (rather than +inf)
means the solver degrades gracefully — if the house *starts* outside the band,
or no control can keep it inside, it still returns the least-violating
best-effort plan and flags the result infeasible rather than returning nothing.

End-of-horizon effect
---------------------
A naive finite horizon with zero terminal cost would drain all stored heat just
before the horizon end (free to end cold, since nothing beyond is costed). The
terminal cost prices the reheat you would owe later:

    terminal_cost(T) = price_ref * max(0, T_ref - T)

with price_ref = mean forecast price over the horizon and T_ref the indoor
target clamped into the comfort band. Raising indoor temperature by dT needs
q*dt summing to dT, so reheating a deficit of (T_ref - T) costs about
price_ref*(T_ref - T) — the terminal cost is that reheat bill, which removes the
incentive to game the horizon boundary.

Trustworthiness gate
--------------------
An MPC plan is only as good as the RC model it is built on, and that model has
not yet had a long real-data validation run. `plan()` always computes a plan
(observing it over time is useful groundwork), but marks it `trustworthy=False`,
with a status of `not_trustworthy`, unless the RC estimate is both mature and
physically plausible:
  * confidence >= min_confidence and accepted_samples >= min_accepted_samples,
  * theta_env > 0 (a real, positive envelope time constant),
  * theta_gain <= -min_gain_magnitude (heat-pump gain clearly negative, i.e. the
    sign that means "colder reading -> more heat"; a near-zero or wrong-sign
    gain means we cannot tell how the pump responds, so any plan is guesswork),
  * theta_solar >= 0, and no parameter pinned at a clip bound (a pinned
    parameter usually means the estimator hit a guardrail rather than
    converging).
The point is to never present a plan as reliable when the underlying model is
not — see the coordinator, which surfaces this on a dedicated status sensor.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MODEL_VERSION = "mpc_dp_v1"

# Comfort-bound enforcement penalty (relative proxy units per degC of
# violation). Large enough to dominate any realistic price*heat stage cost, so
# feasible plans never trade comfort for money, but finite so the solver stays
# numerically well-behaved and degrades gracefully when the house starts
# outside the band. See module docstring.
COMFORT_PENALTY_PER_C = 1.0e6

# How close to a comfort bound the predicted next temperature must land before
# we call that bound the binding constraint (degC).
BAND_TOUCH_C = 0.05


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


# --- configuration / inputs / outputs ----------------------------------------


@dataclass(frozen=True)
class MPCModelParams:
    """The RC-model-derived physical parameters the plan is built from, plus
    the maturity/plausibility metadata used by the trustworthiness gate.

    The coordinator fills these from the latest `RCModelResult` each cycle (so
    the plan always reflects whatever the shadow model currently believes) and
    computes `params_pinned` against the RC model's own clip bounds — kept out
    of this pure module so it need not re-derive rc_model internals.
    """

    theta_env: float
    theta_gain: float
    theta_solar: float
    theta_wind: float = 0.0
    enable_wind: bool = False
    wind_reference_ms: float = 5.0
    confidence: float = 0.0
    accepted_samples: int = 0
    params_pinned: bool = False


@dataclass(frozen=True)
class MPCConfig:
    """Solver tuning. Defaults are production-sane; overridable in tests.

    Only `horizon_hours`, `max_heating_delta_c` and `min_confidence` are
    surfaced as user options (see config_flow.py) — the discretisation
    granularity is kept internal with defaults that keep the DP both accurate
    and cheap (a ~5 degC comfort band at 0.25 degC resolution is ~21 states;
    24 hourly steps × 9 control levels is a few thousand transition
    evaluations, trivially fast in pure Python).
    """

    horizon_hours: float = 24.0
    step_hours: float = 1.0
    temp_grid_step_c: float = 0.25
    control_steps: int = 9
    max_heating_delta_c: float = 8.0
    comfort_min_c: float = 18.0
    comfort_max_c: float = 23.0
    indoor_target_c: float = 21.0
    # Trust gate. confidence saturates fast in the RC model (at its warmup, ~20
    # accepted samples), so min_accepted_samples is the stronger real maturity
    # lever; both must pass.
    min_confidence: float = 1.0
    min_accepted_samples: int = 20
    min_gain_magnitude: float = 0.01
    comfort_epsilon_c: float = 1.0e-6


@dataclass(frozen=True)
class MPCForecasts:
    """Per-step forecast arrays, each already trimmed/padded by the coordinator
    to exactly the number of horizon steps the config implies.

    `valid_steps` is how many leading steps came from a real forecast (price
    and weather) rather than being padded by persistence — surfaced so a short
    real forecast can be reported honestly instead of silently trusted.
    """

    price: tuple[float, ...]
    outdoor_temp_c: tuple[float, ...]
    solar_effect: tuple[float, ...]
    wind_speed_ms: tuple[float, ...]
    valid_steps: int = 0


@dataclass(frozen=True)
class MPCPlanStep:
    """One planned step, retained in full for explainability/attributes."""

    index: int
    time_offset_h: float
    control_delta_c: float          # u chosen for this step
    indoor_temp_c: float            # predicted state at the START of the step
    next_indoor_temp_c: float       # predicted state at the END of the step
    price: float
    heat_delivered_c: float         # q*dt, degC of pump-driven rise this step
    stage_cost: float               # price * heat, relative proxy units
    comfort_violation_c: float      # >0 only if this step leaves the band
    binding: str


@dataclass(frozen=True)
class MPCResult:
    """Explainable per-cycle MPC output. Mirrors HeuristicResult/RCModelResult
    conventions and doubles as the diagnostic sensors' attribute schema."""

    status: str                     # ok|not_trustworthy|infeasible|no_forecast|degenerate|no_data
    trustworthy: bool
    recommended_delta_c: float | None
    horizon_hours: float
    steps_planned: int
    total_cost: float | None        # price cost of the executed plan (proxy units)
    baseline_cost: float | None     # cost of a myopic hold-target plan (proxy units)
    projected_savings: float | None # baseline_cost - total_cost (proxy units)
    binding_constraint: str | None
    predicted_min_temp_c: float | None
    predicted_max_temp_c: float | None
    predicted_next_temp_c: float | None
    predicted_trajectory: tuple[float, ...] | None  # len = steps_planned + 1
    # Echoes of the exact RC parameters this plan was computed from — per the
    # troubleshooting request, so you can see at each cycle which learned
    # values fed the plan without cross-referencing timestamps.
    theta_env: float = 0.0
    theta_gain: float = 0.0
    theta_solar: float = 0.0
    theta_wind: float = 0.0
    time_constant_h: float = 0.0
    confidence: float = 0.0
    accepted_samples: int = 0
    forecast_valid_steps: int = 0
    reason: str = ""
    plan: tuple[MPCPlanStep, ...] = field(default_factory=tuple)
    model_version: str = MODEL_VERSION


# --- small helpers ------------------------------------------------------------


def _controls(config: MPCConfig) -> list[float]:
    """Discrete control (compensation-delta) levels in [-max_heating, 0].

    Always includes 0 (coast) and -max (full heat); >=2 levels guaranteed.
    """
    n = max(2, int(config.control_steps))
    m = abs(config.max_heating_delta_c)
    if m == 0.0:
        return [0.0]
    # From -m (index 0) up to 0 (last index), evenly spaced.
    return [-m + (m) * (i / (n - 1)) for i in range(n)]


def _grid_temps(config: MPCConfig) -> list[float]:
    """Indoor-temperature grid across the comfort band (inclusive)."""
    lo, hi = config.comfort_min_c, config.comfort_max_c
    if hi <= lo:
        return [lo, lo]  # degenerate band; caller flags it
    step = config.temp_grid_step_c if config.temp_grid_step_c > 0 else (hi - lo)
    n = max(2, int(round((hi - lo) / step)) + 1)
    return [lo + (hi - lo) * (i / (n - 1)) for i in range(n)]


def _interp(values: list[float], temp: float, grid: list[float]) -> float:
    """Linear interpolation of a value-table over the (uniform) temp grid, with
    flat extrapolation beyond the ends (used only for start states that are
    already outside the comfort band)."""
    n = len(grid)
    lo, hi = grid[0], grid[-1]
    if temp <= lo:
        return values[0]
    if temp >= hi:
        return values[-1]
    span = hi - lo
    x = (temp - lo) / span * (n - 1)
    i = int(x)
    if i >= n - 1:
        return values[-1]
    frac = x - i
    return values[i] * (1.0 - frac) + values[i + 1] * frac


def _step_temp(temp: float, u: float, t: int, params: MPCModelParams, f: MPCForecasts, dt: float) -> float:
    """Predicted next indoor temperature under control u at forecast step t."""
    t_out = f.outdoor_temp_c[t]
    d = (
        params.theta_env * (t_out - temp)
        + params.theta_gain * u
        + params.theta_solar * f.solar_effect[t]
    ) * dt
    if params.enable_wind:
        wref = params.wind_reference_ms if params.wind_reference_ms > 0 else 5.0
        d += params.theta_wind * (t_out - temp) * (f.wind_speed_ms[t] / wref) * dt
    return temp + d


def _violation(temp: float, config: MPCConfig) -> float:
    eps = config.comfort_epsilon_c
    if temp < config.comfort_min_c - eps:
        return config.comfort_min_c - temp
    if temp > config.comfort_max_c + eps:
        return temp - config.comfort_max_c
    return 0.0


def _best_control(
    temp: float,
    t: int,
    v_next: list[float],
    params: MPCModelParams,
    f: MPCForecasts,
    config: MPCConfig,
    controls: list[float],
    grid: list[float],
):
    """Pick the cost-to-go-minimising control from `temp` at step t.

    Returns (u, t_next, q, stage_price_cost, violation, total_value). `total`
    includes the comfort penalty and the interpolated downstream value; the
    reported `stage_price_cost` excludes the penalty (it is the real proxy
    money cost of this step alone).
    """
    dt = config.step_hours
    price = f.price[t]
    best = None
    for u in controls:
        q = params.theta_gain * u
        t_next = _step_temp(temp, u, t, params, f, dt)
        viol = _violation(t_next, config)
        stage_price = price * max(0.0, q) * dt
        total = stage_price + COMFORT_PENALTY_PER_C * viol + _interp(v_next, t_next, grid)
        if best is None or total < best[5] - 1e-12:
            best = (u, t_next, q * dt, stage_price, viol, total)
    return best


def _terminal_values(grid: list[float], f: MPCForecasts, config: MPCConfig) -> list[float]:
    steps = len(f.price)
    price_ref = (sum(f.price) / steps) if steps else 0.0
    t_ref = _clamp(config.indoor_target_c, config.comfort_min_c, config.comfort_max_c)
    return [price_ref * max(0.0, t_ref - g) for g in grid]


def _describe_binding(
    u0: float,
    t_next0: float,
    viol0: float,
    controls: list[float],
    f: MPCForecasts,
    config: MPCConfig,
) -> str:
    """Human-readable binding constraint for the first (applied) step."""
    if viol0 > config.comfort_epsilon_c:
        return "infeasible: no control keeps indoor temperature within the comfort band"
    u_min = controls[0]
    ctrl_eps = (abs(u_min) / (len(controls) - 1)) * 0.5 if len(controls) > 1 else 0.01
    price0 = f.price[0]
    price_avg = (sum(f.price) / len(f.price)) if f.price else price0
    cheap_now = price0 <= price_avg
    if t_next0 >= config.comfort_max_c - BAND_TOUCH_C:
        return "comfort_max: upper comfort bound limits further pre-heating"
    if t_next0 <= config.comfort_min_c + BAND_TOUCH_C:
        return "comfort_min: adding heat to stay above the comfort floor"
    if u0 <= u_min + ctrl_eps:
        return (
            "price: pre-heating at full authority during a relatively cheap period"
            if cheap_now
            else "price: heating hard now despite the price to protect the comfort floor"
        )
    if abs(u0) <= ctrl_eps:
        return (
            "price: coasting, deferring heat to cheaper hours"
            if not cheap_now
            else "cost-optimal: no heat needed this step"
        )
    return "cost-optimal interior point (no single hard constraint binding)"


def _forward_plan(
    start_temp: float,
    v_tables: list[list[float]],
    params: MPCModelParams,
    f: MPCForecasts,
    config: MPCConfig,
    controls: list[float],
    grid: list[float],
) -> tuple[list[MPCPlanStep], list[float], float]:
    """Roll the optimal policy forward from the actual current temperature.

    Returns (steps, trajectory, total_price_cost). `trajectory` has len
    steps+1 (start temp first). `v_tables[t]` is the cost-to-go table for the
    state entering step t; `v_tables[steps]` is the terminal table.
    """
    steps = len(f.price)
    temp = start_temp
    plan_steps: list[MPCPlanStep] = []
    trajectory = [temp]
    total_price = 0.0
    for t in range(steps):
        u, t_next, heat_c, stage_price, viol, _total = _best_control(
            temp, t, v_tables[t + 1], params, f, config, controls, grid
        )
        binding = _describe_binding(u, t_next, viol, controls, f, config) if t == 0 else ""
        plan_steps.append(
            MPCPlanStep(
                index=t,
                time_offset_h=t * config.step_hours,
                control_delta_c=round(u, 3),
                indoor_temp_c=round(temp, 3),
                next_indoor_temp_c=round(t_next, 3),
                price=round(f.price[t], 4),
                heat_delivered_c=round(heat_c, 4),
                stage_cost=round(stage_price, 4),
                comfort_violation_c=round(viol, 4),
                binding=binding,
            )
        )
        total_price += stage_price
        temp = t_next
        trajectory.append(temp)
    return plan_steps, trajectory, total_price


def _baseline_cost(
    start_temp: float,
    params: MPCModelParams,
    f: MPCForecasts,
    config: MPCConfig,
    controls: list[float],
) -> float:
    """Myopic 'hold the target' strategy: at each step pick the control that
    lands nearest the (clamped) target, ignoring price. Its total price cost is
    the fair no-load-shifting comparison against which MPC savings are quoted.
    """
    dt = config.step_hours
    t_ref = _clamp(config.indoor_target_c, config.comfort_min_c, config.comfort_max_c)
    temp = start_temp
    total = 0.0
    for t in range(len(f.price)):
        best = None
        for u in controls:
            t_next = _step_temp(temp, u, t, params, f, dt)
            err = abs(t_next - t_ref)
            if best is None or err < best[0]:
                best = (err, u, t_next, params.theta_gain * u)
        _err, u, t_next, q = best
        total += f.price[t] * max(0.0, q) * dt
        temp = t_next
    return total


def _untrustworthy_reason(params: MPCModelParams, config: MPCConfig) -> str | None:
    """Return a reason string if the RC estimate is not yet trustworthy, else
    None. Order chosen so the most fundamental problem is reported first."""
    if params.theta_env <= 0:
        return f"envelope parameter non-physical (theta_env={params.theta_env:.4f} <= 0)"
    if params.theta_gain > -config.min_gain_magnitude:
        return (
            f"heat-pump gain not clearly negative (theta_gain={params.theta_gain:+.4f}; "
            f"need <= {-config.min_gain_magnitude:+.4f}) — cannot tell how the pump responds"
        )
    if params.theta_solar < 0:
        return f"solar gain non-physical (theta_solar={params.theta_solar:.4f} < 0)"
    if params.params_pinned:
        return "an RC parameter is pinned at a clip bound (estimator hit a guardrail, not converged)"
    if params.accepted_samples < config.min_accepted_samples:
        return (
            f"RC model still warming up ({params.accepted_samples} accepted samples "
            f"< {config.min_accepted_samples} required)"
        )
    if params.confidence < config.min_confidence:
        return (
            f"RC confidence {params.confidence * 100:.0f}% below required "
            f"{config.min_confidence * 100:.0f}%"
        )
    return None


def plan(
    current_indoor_temp_c: float | None,
    params: MPCModelParams,
    forecasts: MPCForecasts,
    config: MPCConfig = MPCConfig(),
) -> MPCResult:
    """Solve the receding-horizon plan for this cycle. Pure and side-effect free.

    Always returns a populated MPCResult (never raises for ordinary bad
    inputs): missing data / an immature model / an infeasible situation are
    reported via `status`, `trustworthy` and `reason` rather than by throwing,
    so the coordinator's shadow-mode wrapper has nothing to swallow in the
    common cases and the diagnostic sensors always have something to show.
    """
    theta_env = params.theta_env
    tau = 1.0 / theta_env if theta_env > 0 else float("inf")

    def base_result(status: str, reason: str, trustworthy: bool = False) -> MPCResult:
        return MPCResult(
            status=status,
            trustworthy=trustworthy,
            recommended_delta_c=None,
            horizon_hours=config.horizon_hours,
            steps_planned=0,
            total_cost=None,
            baseline_cost=None,
            projected_savings=None,
            binding_constraint=None,
            predicted_min_temp_c=None,
            predicted_max_temp_c=None,
            predicted_next_temp_c=None,
            predicted_trajectory=None,
            theta_env=theta_env,
            theta_gain=params.theta_gain,
            theta_solar=params.theta_solar,
            theta_wind=params.theta_wind,
            time_constant_h=tau,
            confidence=params.confidence,
            accepted_samples=params.accepted_samples,
            forecast_valid_steps=forecasts.valid_steps,
            reason=reason,
        )

    # --- guard clauses --------------------------------------------------------
    if current_indoor_temp_c is None:
        return base_result("no_data", "Indoor temperature unavailable; MPC cannot plan this cycle")
    if config.comfort_max_c <= config.comfort_min_c:
        return base_result(
            "degenerate",
            f"Comfort band is degenerate (min {config.comfort_min_c:.1f} >= "
            f"max {config.comfort_max_c:.1f}); MPC cannot plan",
        )
    steps = len(forecasts.price)
    if (
        steps == 0
        or len(forecasts.outdoor_temp_c) != steps
        or len(forecasts.solar_effect) != steps
        or len(forecasts.wind_speed_ms) != steps
    ):
        return base_result(
            "no_forecast",
            "Missing or mismatched forecast arrays; MPC cannot plan this cycle",
        )

    controls = _controls(config)
    grid = _grid_temps(config)

    # --- backward value iteration --------------------------------------------
    v_tables: list[list[float]] = [[0.0] * len(grid) for _ in range(steps + 1)]
    v_tables[steps] = _terminal_values(grid, forecasts, config)
    for t in range(steps - 1, -1, -1):
        v_next = v_tables[t + 1]
        v_cur = v_tables[t]
        for gi, g in enumerate(grid):
            v_cur[gi] = _best_control(
                g, t, v_next, params, forecasts, config, controls, grid
            )[5]

    # --- forward roll-out from the real current temperature ------------------
    plan_steps, trajectory, total_price = _forward_plan(
        current_indoor_temp_c, v_tables, params, forecasts, config, controls, grid
    )
    baseline_price = _baseline_cost(current_indoor_temp_c, params, forecasts, config, controls)

    recommended = plan_steps[0].control_delta_c
    binding0 = plan_steps[0].binding
    max_viol = max((s.comfort_violation_c for s in plan_steps), default=0.0)
    traj_no_start = trajectory[1:]
    predicted_min = min(traj_no_start) if traj_no_start else None
    predicted_max = max(traj_no_start) if traj_no_start else None
    predicted_next = trajectory[1] if len(trajectory) > 1 else None
    savings = baseline_price - total_price

    # --- trust + status -------------------------------------------------------
    untrust = _untrustworthy_reason(params, config)
    infeasible = max_viol > config.comfort_epsilon_c
    trustworthy = untrust is None and not infeasible

    if infeasible:
        status = "infeasible"
    elif untrust is not None:
        status = "not_trustworthy"
    else:
        status = "ok"

    # --- reason string (explainability, echoing the RC params used) ----------
    gain_echo = (
        f"tau={tau:.1f}h, gain={params.theta_gain:+.3f}, solar={params.theta_solar:.3f}"
    )
    if params.enable_wind:
        gain_echo += f", wind={params.theta_wind:+.3f}"
    reason = (
        f"MPC {config.horizon_hours:.0f}h/{steps}-step plan on RC params [{gain_echo}], "
        f"confidence {params.confidence * 100:.0f}% ({params.accepted_samples} samples): "
        f"recommend {recommended:+.2f}degC delta now "
        f"(indoor {current_indoor_temp_c:.2f} -> {predicted_next:.2f}degC next step); "
        f"binding = {binding0}; horizon cost {total_price:.2f} vs baseline "
        f"{baseline_price:.2f} (savings {savings:+.2f}, relative proxy units)"
    )
    if forecasts.valid_steps < steps:
        reason += (
            f"; only {forecasts.valid_steps}/{steps} steps from real forecast, "
            f"rest extrapolated by persistence"
        )
    if untrust is not None:
        reason += f"; NOT YET TRUSTWORTHY — {untrust}"
    if infeasible:
        reason += f"; INFEASIBLE — max comfort violation {max_viol:.2f}degC in plan"

    return MPCResult(
        status=status,
        trustworthy=trustworthy,
        recommended_delta_c=recommended,
        horizon_hours=config.horizon_hours,
        steps_planned=steps,
        total_cost=round(total_price, 4),
        baseline_cost=round(baseline_price, 4),
        projected_savings=round(savings, 4),
        binding_constraint=binding0,
        predicted_min_temp_c=round(predicted_min, 3) if predicted_min is not None else None,
        predicted_max_temp_c=round(predicted_max, 3) if predicted_max is not None else None,
        predicted_next_temp_c=round(predicted_next, 3) if predicted_next is not None else None,
        predicted_trajectory=tuple(round(t, 3) for t in trajectory),
        theta_env=theta_env,
        theta_gain=params.theta_gain,
        theta_solar=params.theta_solar,
        theta_wind=params.theta_wind,
        time_constant_h=tau,
        confidence=params.confidence,
        accepted_samples=params.accepted_samples,
        forecast_valid_steps=forecasts.valid_steps,
        reason=reason,
        plan=tuple(plan_steps),
    )
