# ClimateOptimizer

A Home Assistant custom integration (HACS) that computes a **compensated outdoor
temperature** for a heat pump's weather-compensation curve, adjusted for:

- Indoor temperature vs. your target (closes the loop — your heat pump usually
  only sees outdoor temperature, not how it's actually going indoors)
- Forecast wind speed (extra heat loss)
- Forecast sun / cloud cover (passive solar gain)
- Electricity price via Nordpool (optional — let indoor temperature drift down,
  within limits you set, during expensive price periods)

ClimateOptimizer does **not** write to your heat pump directly. It publishes a
sensor, `sensor.<name>_compensated_outdoor_temperature`, with the computed value
and full attribute breakdown of *why* it's that value. You wire that sensor into
your heat pump's own external-temperature input (however your specific
integration/automation supports that).

Everything is configured from the Home Assistant UI — no YAML.

### Activation switch (learn mode by default)

Each zone also gets a `switch.<name>_active` entity. **It defaults to off** —
in this "learn mode" state, the sensor publishes the raw outdoor temperature
unmodified (no compensation applied at all), while the heuristic (and the RC
shadow model, below) keep computing normally in the background. The
heuristic's actual recommendation is always visible as the
`recommended_compensated_outdoor_temp_c` attribute, alongside an `active: true/false`
flag, so you can watch what it *would* do before switching it on. Flip the
switch on when you're ready to let it actually influence your heat pump. The
switch's state is restored across Home Assistant restarts.

### Status sensor

`sensor.<name>_status` reports `ok`, `degraded`, or `error`, with attributes
breaking down each source (`outdoor_sensor_ok`, `indoor_sensor_ok`,
`wind_forecast_ok`, `cloud_sun_forecast_ok`, `price_ok` if configured,
`last_error`). `error` means the outdoor sensor (the one required source) is
currently unavailable and the main sensor's value has gone stale; `degraded`
means the update is succeeding but a soft-degraded source (indoor sensor,
wind forecast, cloud/sun forecast, or price) is currently down. Wind and
cloud/sun are tracked separately since not every weather integration
provides both. Unlike every other entity here, this one is always available
— its whole job is to report problems, including when everything else would
otherwise show unavailable.

### Indoor target temperature

`number.<name>_indoor_target_temperature` lets you adjust the target live —
from a dashboard, a schedule, or an automation (e.g. lower it at night or
when away) — without touching the options dialog. It's backed by an
in-memory value rather than a config option, specifically so changing it
doesn't trigger a full reload (which would otherwise reset the RC model's
learning progress every time). Its state is restored across restarts.

## Installation

1. Add this repository to HACS as a custom repository (category: Integration),
   or once published, install directly from HACS.
2. Restart Home Assistant.
3. Settings → Devices & Services → Add Integration → **ClimateOptimizer**.
4. Pick your indoor temperature sensor, an outdoor temperature sensor (a real
   sensor entity — used as the current-temperature baseline, since it's
   generally more accurate than a weather service's estimate), a weather
   entity (used only for its wind/cloud forecast), an optional Nordpool price
   entity, and a starting target indoor temperature (adjustable afterward via
   the `number.<name>_indoor_target_temperature` entity, not this dialog).
5. Tune coefficients, comfort bounds, and the price feature later via the
   integration's **Configure** (options) dialog — no reinstall needed.

## How the value is computed (Phase 1: heuristic)

```
compensated_outdoor_temp = raw_outdoor_temp
    - k_indoor * (indoor_target - indoor_actual)   # colder than target -> read lower -> more heat
    - k_wind   * wind_speed_m_s                     # windier -> read lower -> more heat
    + k_sun    * solar_effect                        # sunnier -> read higher -> less heat
    + price_adjustment                                # expensive price -> read higher -> less heat (optional, bounded)
```

All coefficients, comfort min/max bounds, and price thresholds are adjustable
in the options flow. The sensor's attributes include a per-term breakdown and a
plain-language `reason` string so the output is always explainable.

This is intentionally a simple, transparent heuristic, not a black-box model.

### Heating cutoff (summer guardrail)

At or above a configurable outdoor temperature (`heating_cutoff_c`, default
18°C, options flow), compensation is suppressed entirely and the sensor
publishes the raw outdoor temperature unmodified — no indoor/wind/sun/price
adjustment at all, not even partial credit. Without this, a cold indoor
reading or a windy day could still push the compensated value *below* the raw
temperature even when it's already warm outside, which could trick the heat
pump's own curve into calling for heat on a warm day. `heating_cutoff_engaged`
is exposed as an attribute, and the `reason` string says so explicitly when it
kicks in. Active cooling (a mirrored curve for reversible heat pumps) is
intentionally out of scope — this only ever stops heating, it never starts
cooling.

## RC thermal model (Phase 2: shadow mode only)

A grey-box RC thermal model, fit online from live data via recursive least
squares, runs alongside the heuristic and exposes diagnostic sensors
(thermal time constant, heat-pump gain, solar gain, confidence, prediction
error) — purely for observation. It never influences
`compensated_outdoor_temp_c`; the heuristic above is still what actually runs.
Because the activation switch gates what's *actually applied*, the model only
learns heat-pump gain while the switch is on (it needs real excitation on
that signal) — it can still learn the envelope time constant and solar gain
from passive data while off. A future phase will use this model for a proper
multi-hour cost-optimizing controller once it's proven accurate against real
house data — the heuristic is structured so that can slot in later without
breaking existing sensors/automations.

#### Optional wind term (advanced, off by default)

For houses expected to be genuinely wind-sensitive — old, leaky, exposed —
enable `enable_wind_rc` in options to add a 4th estimated parameter,
`sensor.<name>_rc_model_wind_gain`. It's off by default because for a
typical well-sealed house, wind speed is highly correlated with outdoor
temperature in normal weather data, and a small true wind effect can't be
reliably told apart from that correlation — enabling it just adds estimation
noise for no benefit. A leaky house's true wind sensitivity is large enough
to be statistically distinguishable, which is why this is a per-installation
choice rather than always on or always off. The wind term is an *interaction*
with the temperature gap (`(T_out - T_in) × wind`), not a plain additive
term — wind physically can't cause heat loss with no temperature difference
to amplify — and wind speed is normalised by a configurable reference speed
(`rc_wind_reference_ms`, default 5 m/s) to keep it numerically comparable to
the other terms. Turning this on changes the estimator's dimensionality, so
(like any options change) it triggers a reload and resets learning progress
for a fresh start.

## MPC planner (Phase 3: advisory / shadow mode only)

A Model Predictive Control (MPC) planner runs alongside the heuristic and the
RC model. Each cycle it uses the RC model's *currently learned* physical
parameters plus multi-hour forecasts (electricity price, outdoor temperature,
and — only if the wind term is enabled — wind) to plan a cost-minimising
sequence of compensation deltas over a horizon (default 24 h), subject to your
hard comfort bounds. It uses **receding-horizon control**: it re-solves the
whole plan every cycle with the latest forecasts and only ever surfaces the
*first* step, discarding the rest.

**It is advisory only.** Exactly like the RC model, it never influences
`compensated_outdoor_temp_c` — the heuristic pipeline gated by
`switch.<name>_active` remains the only thing that actually controls anything.
The MPC planner exists so its recommendations can be observed and evaluated
against reality over time, as groundwork before anyone trusts it to run heating.

### What it does and how

The solver is **dynamic programming over a discretised indoor-temperature
state** (chosen over LP/QP: no scipy dependency, lightweight, and inherently
explainable — you can see exactly which constraint binds). A backward
value-iteration pass computes the cost-to-go at every (time, temperature) node
using the RC model's dynamics; a forward pass from the current indoor
temperature reads off the optimal control sequence. Comfort bounds are enforced
as a dominating penalty, so a feasible plan never violates them; if the house
starts outside the band or can't be held, it returns a least-violating
best-effort plan and flags itself `infeasible` rather than failing.

The planner is **heating-only** (it adds heat or coasts, never commands
cooling — mirroring the summer-cutoff philosophy). Costs are in **relative
proxy units** (price × °C), useful for ranking plans and reporting
savings-vs-baseline, not a currency figure — the absolute thermal scale isn't
identifiable from indoor-temperature dynamics alone. Savings are quoted against
a myopic "hold the target" baseline, so they reflect both load-shifting (heat
banked into the comfort band before price spikes) and energy-minimisation
(riding cooler within comfort when price is flat).

### Trustworthiness gate

An MPC plan is only as good as the RC model under it, and that model hasn't yet
had a long real-data validation run. The planner **always** computes a plan
(observing it is useful), but marks it *not yet trustworthy* unless the RC
estimate is both mature (confidence ≥ `mpc_min_confidence` and enough accepted
samples) and physically plausible (positive envelope time constant, a clearly
negative heat-pump gain, non-negative solar gain, and no parameter pinned at a
clip bound). The point is to never present a plan as reliable when the
underlying model isn't.

### Sensors

- `sensor.<name>_mpc_recommended_compensation_delta` — the recommended
  first-step delta. Its attributes carry the whole plan: the `reason` string,
  the `binding_constraint`, projected cost/savings, the predicted
  indoor-temperature `predicted_trajectory`, the full per-step `plan`, the
  trust gate result, and **explicit echoes of the RC parameters** (gain, tau,
  solar, wind if enabled) the plan was actually computed from this cycle — so
  you can troubleshoot *why* a plan looks the way it does. (The live RC gain /
  tau / solar / wind sensors from Phase 2 track the same values continuously.)
- `sensor.<name>_mpc_status` — `ok`, `not_trustworthy`, `infeasible`,
  `no_forecast`, `no_data`, or `misconfigured`, with `trustworthy`,
  `binding_constraint` and forecast-coverage details in attributes.
- `sensor.<name>_mpc_projected_savings` — projected horizon savings vs the
  hold-target baseline, in relative proxy units.
- `sensor.<name>_mpc_planned_next_indoor_temperature` — the indoor temperature
  the plan predicts at the end of the first step.

### Options

Three advisory-only options (Configure dialog): the planning horizon
(`mpc_horizon_hours`, default 24 h), the assumed heating authority
(`mpc_max_heating_delta_c`, default 8 °C), and the minimum RC confidence for a
plan to be reported trustworthy (`mpc_min_confidence`, default 1.0). The
state-space granularity is fixed at sensible internal defaults.

### Known limitations (advisory-only groundwork)

- **Solar is not yet forecast over the horizon** (that would need per-hour
  future sun elevation): the planner assumes no solar gain across the horizon,
  which is the comfort-safe direction (it never counts on free heat it might
  not get). This is a natural next enhancement.
- If the weather/price forecast is shorter than the horizon, the last value is
  held (persistence); `forecast_valid_steps` and the `reason` string report how
  many leading steps were real forecast.
- Because the RC model treats a zero compensation delta as *no* heat-pump
  contribution (the pump's own baseline weather curve isn't separately
  modelled), the plan's *absolute* predicted temperatures are in the model's
  reference frame, not real wall-thermometer degrees. The *relative* decisions
  (when to spend heat given prices and thermal storage) are what's meaningful —
  another reason this stays advisory-only for now.

## Local history logging (optional, off by default)

`enable_data_logging` (options flow) appends one JSON line per update cycle
to `/config/climate_optimizer_data/<entry_id>.jsonl` — the raw physical
inputs (indoor/outdoor temp, wind, solar effect, price) plus the computed
heuristic/RC/MPC results for that cycle. Purely local; nothing is
transmitted anywhere. This exists because Home Assistant's own recorder
purges history by default (commonly ~10 days) and its long-term statistics
only keep hourly aggregates — too coarse to properly re-fit the RC model or
backtest an MPC change later. With this on, real history survives and can be
replayed offline through a candidate model change without waiting for new
live data. The resolved file path is shown on `sensor.<name>_status`'s
`data_log_path` attribute whenever logging is on.

Whenever MPC actually runs a cycle, the record also embeds the exact
multi-hour forecast it planned against (`mpc_forecast_price`,
`mpc_forecast_outdoor_temp_c`, `mpc_forecast_wind_speed_ms`,
`mpc_forecast_solar_effect`, plus `mpc_horizon_hours`/`mpc_step_hours` and
`mpc_forecast_valid_steps`) — not just the realised/actual values. This
matters because forecasts get revised over time; the realised outcome isn't
a substitute for what was actually known at decision time, so faithfully
replaying or backtesting a past MPC plan needs the forecast snapshot, not
just hindsight. Logged at whatever `mpc_horizon_hours` is currently
configured (not a separate fixed window), so it always matches what the
live solver is actually doing.

## License

MIT — see [LICENSE](LICENSE).
