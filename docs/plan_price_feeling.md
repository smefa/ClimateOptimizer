# Plan: Pluggable "Price Feeling" module

**Goal:** decide, on a continuous/graded scale, how expensive *right now* is, and
separately whether dampening is *worth it in money* — then throttle heat only on
the genuinely worst, worthwhile periods. `comfort_min_c` (~18°C) stays an
unconditional floor throughout, untouched by any of this.

## What already exists (extract, don't rebuild)

`heuristic.py` already has the raw material, just not modularized:

- `_price_band_info()` — today's median/peak from the day-ahead forecast, with
  an "engaged" gate (`spread/median ≥ 0.2`) and a response band.
- `price_significance()` — relative (today vs 30-day rolling spread, via
  `PriceSpreadHistory`) + absolute (spread vs money floor) taper.
- `_percentile()` — generic interpolation helper, currently unused for a live
  feature.
- `comfort_min_c` applied exactly once, unconditionally, at the end of
  `compute()` — no price-feeling logic should ever touch this.

## 1. Common interface

New module `custom_components/truetemp/price_feeling.py`:

```python
@dataclass
class PriceFeelingResult:
    score: float          # 0.0 (cheap) .. 1.0 (most expensive)
    engaged: bool          # only True on the genuinely worst prices
    percentile: float | None = None
    label: str | None = None

class PriceFeelingModel(Protocol):
    def evaluate(self, inputs: PriceFeelingInputs) -> PriceFeelingResult: ...
```

`compute()` calls `model.evaluate(...)` and uses `.score`/`.engaged` wherever
`price_significance_factor`/`_band_response` are used today.

## 2. Scoring models (answer "how expensive does this feel")

| Model | Logic |
|---|---|
| `relative_band` (default) | Today's median+spread band — direct extraction of current code, zero behavior change on upgrade |
| `percentile` | Rank current price against today's (or rolling N-day) forecast via `_percentile()`; `engaged` above a configurable percentile (e.g. top 25%) |
| `rolling_zscore` | Current price vs rolling mean/stddev (reuse `PriceSpreadHistory` pattern) — better for volatile markets |
| `absolute_threshold` | Flat user-set cents/kWh cutoff, no forecast dependency |

Each owns its own strictness knob (percentile cutoff, z-score threshold, spread
ratio, cents value) — that's what enforces "only the most expensive parts."

## 3. `save_at_least` — a savings gate, applied on top of whichever scoring model is active

Answers a different question than the models above ("is this worth the comfort
trade-off"), so it's wired as a **secondary gate**, not a peer model — composes
with any of the four above rather than replacing one.

```
premium_per_kwh   = current_price - baseline_price          # from _price_band_info median
saving_per_degree = premium_per_kwh * kwh_per_degree_c       # currency/°C, computed live
projected_saving  = saving_per_degree * candidate_sag_c      # candidate_sag_c = the active model's proposed sag
engaged           = projected_saving >= CONF_MIN_SAVING_AMOUNT
```

- `CONF_KWH_PER_DEGREE_C` — one calibrated constant (kWh saved per °C of sag), a
  stable physical property of the house+heater, independent of currency/price
  regime. No nameplate power rating, no duration estimate — reuses whatever sag
  the active scoring model already proposed.
- MVP calibration: manual config field with guidance (compare meter/bill during
  a known sag vs a comparable non-sag period). Later, optional: learn it
  empirically from an existing power sensor the same way `learner.py` learns
  the offset→temperature relationship, falling back to the manual constant when
  no power sensor is configured.
- If `engaged` is false, `price_shift_c` is zeroed/tapered for that cycle
  regardless of what the scoring model wanted.

## 4. Anti-flapping

Any discrete `engaged` output (scoring model or the savings gate) must use the
same hysteresis pattern as `resolve_heating_hard_limit_engaged()` (the fix in
`808105f`/`a7da838`) — thread `prev_engaged` from the coordinator's last
`HeuristicResult`, apply a deadband. Otherwise this reintroduces the exact
flapping bug just fixed.

## 5. Config surface

- `CONF_PRICE_FEELING_MODEL` — `SelectSelector` in `async_step_price`, default
  `relative_band` (no behavior change for existing installs).
- Model-specific params shown conditionally, following the `CONF_OUTPUT_MODE`
  branching pattern already in `config_flow.py`.
- `CONF_ENABLE_SAVING_GATE` + `CONF_KWH_PER_DEGREE_C` +
  `CONF_MIN_SAVING_AMOUNT` — separate optional block, independent of model
  choice.
- Model *choice* = setup-time config option; per-model aggressiveness threshold
  = live-tunable `select`/`number` entity, following the `price_comfort_tier`
  precedent, so users can retune without a reload.

## 6. Visibility

Sensor attributes: `price_feeling_model`, `price_feeling_score`,
`price_feeling_percentile`, `saving_gate_engaged`, `projected_saving` —
alongside existing `price_band_start`/`price_median`/`price_significance_factor`
(`coordinator.py` ~L1180). Surface on `truetemp-card.js` so it's visible *why*
the heater is or isn't coasting.

## 7. Testing

- `tests/test_price_feeling.py`: one class per scoring model (flat-price day,
  single-spike day, missing/short forecast, comfort floor never violated) + a
  class for the savings gate (zero premium, premium below/above threshold,
  `kwh_per_degree_c = 0`).
- Regression test: default `relative_band` output must exactly match today's
  pre-refactor `heuristic.compute()` output on existing `test_heuristic.py`
  fixtures.
- `test_control_loop.py`: coordinator swaps models via config and still
  produces a valid `HeuristicResult`; savings gate correctly zeroes
  `price_shift_c` when under threshold.

## 8. Phasing

1. Extract current logic into `relative_band` behind the new interface — all
   existing tests pass unmodified.
2. Wire config flow model selector + coordinator instantiation.
3. Add `percentile` model.
4. Add `save_at_least` gate (manual `kwh_per_degree_c`).
5. Add `rolling_zscore`, then `absolute_threshold` if still wanted.
6. Sensor attributes + card UI indicator.
7. (Optional, later) learn `kwh_per_degree_c` from a power sensor if
   configured.
