# ClimateOptimizer

## NOT READY FOR PRODUCTION

A Home Assistant custom integration (HACS) that computes a **compensated outdoor
temperature** for a heat pump's weather-compensation curve, adjusted for:

- Indoor temperature vs. your target (closes the loop — your heat pump usually
  only sees outdoor temperature, not how it's actually going indoors)
- Forecast wind speed (extra heat loss)
- Forecast sun / cloud cover (passive solar gain)
- Electricity price via Nordpool (optional — let indoor temperature drift down,
  within limits you set, during expensive price periods)

ClimateOptimizer publishes a sensor,
`sensor.<name>_compensated_outdoor_temperature`, with the computed value and a
full attribute breakdown of *why* it's that value. By default it does **not**
write to your heat pump directly — you wire that sensor into your heat pump's
own external-temperature input if possible, or using special dedicated
hardware like OhmOnWifi/OhmigoWifi.

Optionally, via **Configure**, you can also have it push the value itself
every cycle — either to a `number.*` entity (e.g.
`number.nibe_ohmigo_temperature`), or directly to an OhmOnWifi/Ohmigo
device's own local API by hostname/IP, bypassing any HA entity entirely —
instead of you having to wire it up with a separate automation. Both are off
by default; see "Optional: push the value automatically" below.

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

Think of the switch as a **training/live toggle**, not just a safety cutout.
With it *off*, the published value equals the raw outdoor temperature exactly —
a true no-op that can never behave worse than your heat pump's built-in
weather-compensation curve did before this integration was installed, which is
why off is the safe universal default. But "off" is not *purely* a fallback:
the RC shadow model can only learn your heat pump's **gain** (how strongly a
compensation nudge moves indoor temperature) while the switch is *on*, because
that is the only time a real, deliberate compensation delta is actually applied
to excite that signal (see the RC model section). Switching to live mode is
therefore also what lets the model calibrate itself to your specific house —
it is opt-in training, with the understood trade-off that the heuristic's
fixed coefficients are uncalibrated until then and could respond sluggishly or
oscillate on some houses while it does. The direction of any correction is
always right (negative feedback toward your indoor target); only the magnitude
is uncertain before calibration.

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

### Optional: push the value automatically

Via **Configure**, there are two independent, headed sections for having
ClimateOptimizer push its value out itself instead of you wiring up a
separate automation — independent meaning both can be set at once and both
get pushed to every cycle, not an either/or choice. Both mirror exactly what
the main sensor is currently publishing — the raw outdoor temperature while
the activation switch is off/learn-mode, the compensated value once it's on
— so this is never a second, independently-gated output. Each channel
separately skips its own repeat push when its value hasn't moved by more
than 0.05°C since the last one it sent, so a real device register isn't
rewritten every cycle for no reason. A failed push on either channel is
logged as a warning and otherwise ignored, independently of the other —
neither ever affects `sensor.<name>_compensated_outdoor_temperature` itself.

- **"OhmOnWifi direct API"** (`ohmonwifi_host`): the device's hostname or
  IP — e.g. its mDNS default `ohmonwifi.local` for a stock, unrenamed
  device, or an IP if you've renamed it or mDNS doesn't resolve reliably on
  your network. Unset (disabled) by default. When set, every cycle
  ClimateOptimizer calls the device's own local HTTP API directly
  (`http://<host>/AT/?T=<value>`, per Ohmigo's published API doc), with no
  Home Assistant entity in between. Saving the options dialog does a
  one-time live check against the device's `/info` endpoint and rejects the
  save with an error if it can't reach it (typo'd address, device off,
  wrong network) — it does not re-validate on every subsequent update cycle
  after that.
- **"Push to a number entity"** (`output_number_entity`): a `number.*`
  entity belonging to another integration — for example
  `number.nibe_ohmigo_temperature` if you've set up OhmOnWifi as a HA number
  entity yourself instead of using the direct option above. Every cycle
  calls `number.set_value` on it. Unset (disabled) by default. Not validated
  at save time (HA already guarantees the entity exists, since it's picked
  from a live entity list).

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
   the `number.<name>_indoor_target_temperature` entity, not this dialog). An
   optional heat-pump power sensor can be added later via **Configure** — see
   "Local history logging" below.
5. Tune coefficients, comfort bounds, and the price feature later via the
   integration's **Configure** (options) dialog — no reinstall needed.

## Dashboard: bundled feature-test card

The integration ships a small custom Lovelace card,
`custom_components/climate_optimizer/www/climate-optimizer-card.js`, and
registers it as a frontend resource automatically on startup — there's nothing
to add under Settings → Dashboards → Resources.

It's a diagnostic/demo card, not a polished production widget: it exists to
prove every sensor/number/switch/select a zone exposes is reachable from a
dashboard, and to make the Phase 2/3 shadow-mode output (RC model params, the
MPC plan and its predicted trajectory) inspectable at a glance without digging
through entity attributes. Add it to a dashboard with:

```yaml
type: custom:climate-optimizer-card
entity: sensor.<name>_status
```

Any single entity belonging to the zone works — the card discovers the rest of
that zone's entities itself via the frontend entity registry, so it stays
correct even if you rename entities, and shows a `Feature test: N/20 known
ClimateOptimizer entities discovered` line as a self-check. All 20 entities
exist regardless of configuration (e.g. the wind/power sensors just read
unavailable or pinned when their optional source isn't set up), so this
should normally read 20/20; a lower count means the card couldn't reach the
frontend entity registry and fell back to showing just the one configured
entity.

## How the value is computed (Phase 1: heuristic)

```
compensated_outdoor_temp = raw_outdoor_temp
    - k_indoor * (indoor_target - indoor_actual)   # colder than target -> read lower -> more heat
    - k_wind   * wind_speed_m_s                     # windier -> read lower -> more heat
    + k_sun    * solar_effect                        # sunnier -> read higher -> less heat
    + price_adjustment                                # expensive price -> read higher -> less heat (optional, bounded)
```

All coefficients, comfort min/max bounds, and price thresholds are adjustable
in the options flow, which is organised as a menu of focused pages (Sensors and
inputs / Comfort / Tuning / Price and savings / Output push / Advanced). Each
page saves independently. The sensor's attributes include a per-term breakdown,
a `total_adjustment_c` summing it up (the recommended shift from the raw
outdoor reading, after the output sanity clamp), and a plain-language `reason`
string so the output is always explainable.

This is intentionally a simple, transparent heuristic, not a black-box model.

### Optional inputs: sun and wind

Both weather-derived terms can be switched off individually on the **Sensors and
inputs** page. Switching one off is not the same as setting its coefficient to
zero: the term is removed from the thermal model as well, so the estimator stops
carrying a dimension it will never see excitation on (which, left in place,
slowly inflates its share of the covariance budget — see the RC model section).

The weather entity is only consumed by these two terms, so it is optional; with
both inputs off, none is needed and the per-cycle forecast calls are skipped
entirely.

## Auto-tuning (deriving the coefficients from your house)

`k_indoor`, `k_wind`, `k_sun` and `k_price` are expressed in "compensated-outdoor
°C per unit of input" — a unit that is specific to one house, and in fact to one
*outdoor temperature*, because a heat pump's weather curve is not linear. One
degree of spoofing buys a different amount of heat at −15 °C than at +5 °C (the
curve steepens in cold while the compressor simultaneously nears its capacity
limit). No single hand-typed constant can be right across a season.

The RC model already estimates the plant, so these can be derived instead of
asked for. Set the **Tuning mode** entity to `auto` and the controller inverts
the model's own control channel:

| coefficient | derived as | reasoning |
| --- | --- | --- |
| `k_indoor` | `1 / (tau_cl · \|theta_gain\|)` | close an indoor error with time constant `tau_cl` |
| `k_sun` | `theta_solar / \|theta_gain\|` | back off by exactly the measured solar gain |
| `k_wind` | `theta_wind · \|T_out−T_in\| / (wind_ref · \|theta_gain\|)` | offset the measured wind loss |
| `k_price` | `3.3 / (tau_cl · \|theta_gain\|)` | same law, with deliberately asymmetric braking authority |

Non-linearity is then handled for free: every coefficient is divided by the
*currently estimated* `theta_gain`, so when the curve steepens and the gain
grows, the coefficients shrink by the same factor.

`tau_cl` (how fast errors should close) is itself derived, not asked for:
`tau_cl = clamp(tau_open / 3, emitter_floor, 24 h)`, where `tau_open = 1/theta_env`
is already the best-estimated parameter in the model. The speedup ratio of 3 is
dimensionless, which is what makes it legitimate to hardcode where `k_indoor`
was not. The **heating system** question (underfloor vs. radiators) sets the
`emitter_floor` — the 1R1C model has no transport delay, so this is the one place
the physical lag of the emitters enters the calculation.

The deep-cold price taper is also replaced. Rather than three hand-drawn outdoor
thresholds, Auto mode asks whether the sag is *recoverable*: at full heating
authority the net recovery rate is `|theta_gain|·u_max + theta_env·(T_out−T_in)`,
and the envelope term eats into it the colder it gets. The taper shape therefore
falls out of this house's own physics.

### Safety: a ramp, not a switch

Every formula above divides by `theta_gain`, so a bad estimate does not merely
detune the loop. Three hard gates and one soft ramp guard it. The derived values
fall back to your configured ones whenever the heat pump has not yet been
excited (no compensation delta has ever been applied, so there is no gain to
invert), `|theta_gain|` is below its floor, or any RC parameter is pinned at a
clip bound. Even once all three pass, the derived values are blended in
proportionally to accumulated evidence over ~5 days of accepted samples.

This is why Auto is the default: at zero evidence it is *exactly* Manual mode,
and it diverges only as the model earns it.

### Comparing before committing

The derivation runs in **both** modes. `sensor.*_auto_tune_blend` reports the
blend weight as a percentage, and its attributes carry the full side-by-side —
every manual value, its derived counterpart, and the value actually in force.
In Manual mode those derived figures are purely advisory, so you can watch them
track your house for a season and decide whether they look sane before flipping
the switch.

Note that the blend weight is the *readiness* of the derivation, not the active
mode: in Manual mode it can sit at 100% while none of the derived values are
being used. The `tuning_mode` attribute is what says which set is driving the
output.

### Troubleshooting and logging

Three layers, answering different questions:

**Live sensors** — `sensor.*_auto_tune_blend` carries the full side-by-side in
its attributes, and `sensor.*_auto_tune_effective_k_indoor` is a first-class
sensor so the loop gain can be graphed directly. That second one is the number
to watch: it's inversely proportional to the estimated heat-pump gain, so it's
exactly what moves across a season on a non-linear curve, and misbehaving
auto-tuning shows up there as drift or oscillation long before it shows up as a
comfort complaint. It reports the effective value in both modes, so the trace
stays continuous when you flip between Manual and Auto.

**Download diagnostics** (the button on the integration's entry page) — a
complete cross-sectional snapshot: config actually in force, live runtime values
that aren't config (activation switch, tuning mode, target, tier), all four
models' latest results, the resolved auto-tune constants, and the **raw RLS
covariance matrix**. That last one appears on no sensor and is what
distinguishes "still converging" from "covariance has wound up and the fit has
stopped responding". The output-push targets are redacted; source entity ids are
not, since they're often the actual problem.

**JSONL history log** (opt-in, Advanced page) — every cycle records the manual,
derived, *and* effective coefficients. The effective ones matter most: in Auto
mode they move every cycle and are not inferable from the stored config, so
without them an offline replay can't reconstruct what the controller was doing.
When the derivation is blocked, the reason is recorded too (only then — logging
the full reason string every cycle would dominate the file for something
reconstructible from the numbers).

That third layer is what answers the question this feature exists for: pair
`rc_theta_gain` against `raw_outdoor_temp_c` over a season and you can see
directly whether your heat curve's non-linearity is real and how large it is,
rather than assuming it.

Set the `custom_components.climate_optimizer` logger to `debug` for a
per-cycle `reason` line from each model.

### What is never auto-tuned

Comfort min/max, the target temperature, the summer cutoff and the price tier.
No measurement of a building can tell you what its occupant prefers. The COP
half of the cold taper is also deliberately not modelled: heat bought back at
−15 °C genuinely costs more per kWh, but the only power signal available is
contaminated by hot-water production (see the logging section), so rather than
invent a COP curve the derived taper covers recovery feasibility only.

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
learns heat-pump gain while a real compensation delta is being applied (it
needs real excitation on that signal) — it can still learn the envelope time
constant and solar gain from passive data the rest of the time. A future phase
will use this model for a proper multi-hour cost-optimizing controller once
it's proven accurate against real house data — the heuristic is structured so
that can slot in later without breaking existing sensors/automations.

**The heat-pump gain is only added to the model once it's actually been
excited.** The gain parameter's only driver is the compensation delta that was
*really applied*, which is zero whenever the activation switch is off *or* the
summer heating-cutoff has kicked in (compensated equals raw, so nothing is
applied even with the switch on). If the model carried a gain parameter during
such an idle stretch — potentially the whole warm half of the year — that
never-excited parameter's uncertainty would balloon every cycle (a property of
the recursive-least-squares forgetting factor) and, after about two weeks,
trip an internal covariance safeguard that then drags down confidence in the
envelope and solar parameters that *are* being learned correctly from passive
weather. So the estimator simply doesn't include the gain parameter until the
first time a genuinely non-zero applied delta reaches it; at that point the
gain dimension is added while every already-learned parameter (time constant,
solar, wind) and its confidence is preserved exactly. Until then, the heat-pump
gain sensor reads *unavailable* (honestly "not modelled yet", as distinct from
a learned value of zero), and the MPC planner below reports "heat pump has not
yet been excited" as its own distinct not-trustworthy reason. Once added, the
gain parameter stays for good. (Known limitation, left for future work: if the
switch is turned back off for another long idle stretch *after* gain has been
added, that same slow uncertainty build-up can recur on the now-present gain
dimension; solving it properly needs selective per-parameter forgetting, a more
invasive change deferred on purpose.)

The model's learned state — its parameter estimates, the RLS covariance
matrix, whether the gain dimension has been added yet, and the
warmup/confidence and accepted/rejected counters — is **persisted across Home
Assistant restarts and reloads**. It's written to HA's local storage (a
debounced JSON store keyed by the config entry, so renaming a zone never
orphans its learning), reloaded before the first cycle after a restart, and
flushed on unload. Without this the estimator reset to a cold-start prior on
every restart or deploy, which threw away accumulated learning and — observed
in practice — could let the time constant drift up into its clip ceiling after
frequent restarts, making a full heating season of learning impossible if
restarts were at all common. Persistence is strictly additive and defensive:
an empty, corrupt, version-mismatched, or wrong-dimensionality store is
silently discarded in favour of a clean cold start, and any storage error is
logged and swallowed — it can never break or delay the real published output.
The stored state records the exact layout it was fit under — whether the solar
and wind dimensions were present, and whether the gain dimension had been added
— rather than trying to infer the layout from the parameter count. Counting is
not sound here: with both optional dimensions available, several different
layouts share a length (envelope + wind + gain and envelope + solar + gain are
both three wide), so a count check alone would let a state load into the wrong
layout and silently reinterpret a learned wind coefficient as solar. Note that
toggling either optional input resets learning on purpose, because it changes the
estimator's shape and the old saved state no longer matches; states saved by
versions before the lazy-gain change are also discarded and cold-started, by
design.

#### Optional wind term (advanced, off by default)

For houses expected to be genuinely wind-sensitive — old, leaky, exposed —
enable `enable_wind_rc` on the Advanced page to add an estimated wind parameter,
`sensor.<name>_rc_model_wind_gain`. This is separate from, and gated behind, the
wind input toggle on the Sensors page: that one says "wind is available and worth
compensating for", this one opts into the statistically riskier business of
*estimating* a wind coefficient. It's off by default because for a
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

Once the active file reaches 10 MB it's rotated: gzipped to a timestamped
sibling (`<entry_id>.<UTC timestamp>.jsonl.gz`) and a fresh `<entry_id>.jsonl`
starts logging from empty. Rotated files are never deleted automatically —
past data stays intact, just compressed; `zcat`/`gunzip` (or `gzip.open` in
Python) reads them directly as JSONL.

If an optional heat-pump power sensor is configured (Configure → "Heat pump
power sensor"), each logged record also gets `power_w` (the raw reading,
echoed live on `sensor.<name>_power_draw`) and a coarse `cycle_energy_kwh` /
`cycle_cost` estimate (power held constant since the previous logged cycle,
times the current price) — enough to compute a real cost trend offline,
instead of MPC's relative proxy-unit "savings" figure. **Important:** on many
installs the power sensor is shared with hot water production, so this figure
is NOT attributable to space heating alone. It's still useful for comparing
tuning changes (tier, wind term, k_price) against each other over matched
time windows, since hot-water usage is independent of those settings and
averages out — just don't read it as "the compensation saved €X".

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
