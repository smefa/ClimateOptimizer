#!/usr/bin/env python3
"""Offline validation harness for the RC thermal model (rc_model.py).

Reads the local JSONL history that `data_logger.py` writes (opt-in, see
CONF_ENABLE_DATA_LOGGING) and answers the question this project has been
carrying since Phase 2: *is the RC model actually any good?*

Run:
    python tools/validate_rc_model.py <entry_id>.jsonl [more.jsonl.gz ...]

Options:
    --enable-wind        replay/fit with the optional wind regressor
    --wind-ref M         wind reference speed (default 5.0 m/s)
    --train-frac F       holdout split point (default 0.7)
    --horizons H,H,...   open-loop rollout horizons in hours (default 1,3,6,12)
    --min-outdoor C      drop cycles warmer than this (summer/venting filter)
    --exclude-cutoff     drop cycles where the heating cutoff was engaged

Zero third-party dependencies, matching the pure modules it validates.

Why each section exists
-----------------------
The headline risk is *not* "does RLS converge" — it is that a 15-minute-cadence
indoor temperature barely moves, so almost any model scores a small one-step
error. Every accuracy number here is therefore reported against a **persistence
baseline** (predict no change). A model that cannot beat persistence has learned
nothing useful, however pretty its convergence plot.

The second risk is identifiability. `theta_gain` in particular has never had
real excitation (the activation switch spent most of the project off), and the
optional wind term is collinear with the envelope term by construction. Section
3 reports VIFs, standard errors and t-statistics so "we estimated a number" is
never confused with "we measured something".

The third risk is that MPC does not use one-step predictions at all — it rolls
the model out over hours. Section 5 tests exactly that: free-running open-loop
simulation over multi-hour horizons, again against persistence.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "truetemp")
)

import rc_model  # noqa: E402  (path shim above is deliberate)

# --- loading -----------------------------------------------------------------


def _open(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def load_records(paths: list[Path]) -> list[dict]:
    """Load every JSONL line from the given files, sorted by timestamp.

    Rotated `.jsonl.gz` siblings are accepted directly, so a full history can
    be replayed by globbing the whole data directory. Malformed trailing lines
    (a crash mid-append) are skipped rather than aborting the run.
    """
    records: list[dict] = []
    for path in paths:
        bad = 0
        with _open(path) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    bad += 1
        if bad:
            print(f"  note: skipped {bad} malformed line(s) in {path.name}")
    records.sort(key=lambda r: r.get("ts", ""))
    # Drop exact duplicate timestamps (overlapping files given twice).
    deduped: list[dict] = []
    seen: set[str] = set()
    for rec in records:
        ts = rec.get("ts", "")
        if ts and ts in seen:
            continue
        seen.add(ts)
        deduped.append(rec)
    return deduped


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


# --- sample construction -----------------------------------------------------


@dataclass(frozen=True)
class Sample:
    """One fittable transition: conditions at t -> realised indoor change to t+1.

    Mirrors `rc_model.step()` exactly — the regressors are driven by the
    *previous* cycle's conditions held over dt, and the target is the realised
    indoor delta. Getting this wrong (e.g. using the current cycle's outdoor
    temp) would flatter the model with information it does not have live.
    """

    ts: datetime
    dt_h: float
    y: float                 # realised indoor change over dt (degC)
    temp_gap: float          # T_out - T_in at the start of the interval
    u: float                 # applied compensation delta at the start
    solar: float
    wind_ms: float
    indoor_start: float
    outdoor_start: float
    heating_cutoff: bool
    is_active: bool


def build_samples(records: list[dict]) -> tuple[list[Sample], dict]:
    """Turn consecutive log records into fittable transitions.

    Applies the same hard gates `rc_model.step()` applies live (indoor
    availability, finite inputs, dt bounds, implausible one-cycle swing), so
    the offline sample set matches what the live estimator would have accepted.
    The adaptive residual-sigma gate is deliberately NOT applied here — it
    depends on estimator state, and applying it during batch fitting would let
    the model discard the data that disagrees with it.
    """
    stats = {
        "total": len(records),
        "no_indoor": 0,
        "non_finite": 0,
        "dt_out_of_range": 0,
        "implausible_step": 0,
        "usable": 0,
    }
    samples: list[Sample] = []
    prev: dict | None = None
    prev_ts: datetime | None = None

    for rec in records:
        ts = _parse_ts(rec.get("ts", ""))
        indoor = rec.get("indoor_temp_c")
        available = rec.get("indoor_data_available", False)
        if ts is None or not available or indoor is None:
            stats["no_indoor"] += 1
            # An unusable record breaks the chain: the next transition would
            # otherwise span an unmeasured interval.
            prev, prev_ts = None, None
            continue

        if prev is not None and prev_ts is not None:
            dt_s = (ts - prev_ts).total_seconds()
            values = (
                indoor,
                prev["indoor_temp_c"],
                prev.get("raw_outdoor_temp_c"),
                prev.get("applied_delta_c", 0.0),
                prev.get("solar_effect", 0.0),
                prev.get("wind_speed_ms", 0.0),
            )
            if any(v is None or not math.isfinite(v) for v in values):
                stats["non_finite"] += 1
            elif dt_s < rc_model.MIN_DT_SECONDS or dt_s > rc_model.MAX_DT_SECONDS:
                stats["dt_out_of_range"] += 1
            else:
                y = indoor - prev["indoor_temp_c"]
                if abs(y) > rc_model.ABS_MAX_INDOOR_STEP_C:
                    stats["implausible_step"] += 1
                else:
                    samples.append(
                        Sample(
                            ts=ts,
                            dt_h=dt_s / 3600.0,
                            y=y,
                            temp_gap=prev["raw_outdoor_temp_c"] - prev["indoor_temp_c"],
                            u=prev.get("applied_delta_c", 0.0),
                            solar=prev.get("solar_effect", 0.0),
                            wind_ms=prev.get("wind_speed_ms", 0.0),
                            indoor_start=prev["indoor_temp_c"],
                            outdoor_start=prev["raw_outdoor_temp_c"],
                            heating_cutoff=bool(prev.get("heating_cutoff_engaged", False)),
                            is_active=bool(prev.get("is_active", False)),
                        )
                    )
                    stats["usable"] += 1
        prev, prev_ts = rec, ts

    return samples, stats


def design_row(sample: Sample, layout: tuple[str, ...], wind_ref: float) -> list[float]:
    """The regressor vector for one sample, in the given layout order."""
    return rc_model._regressors(
        sample.temp_gap,
        sample.u,
        sample.solar,
        sample.wind_ms / wind_ref,
        sample.dt_h,
        layout,
    )


# --- small dense linear algebra (stdlib only, N <= 4) ------------------------


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting. None if singular."""
    n = len(rhs)
    aug = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-14:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        for row in range(col + 1, n):
            factor = aug[row][col] / aug[col][col]
            for k in range(col, n + 1):
                aug[row][k] -= factor * aug[col][k]
    out = [0.0] * n
    for row in range(n - 1, -1, -1):
        total = aug[row][n] - sum(aug[row][k] * out[k] for k in range(row + 1, n))
        out[row] = total / aug[row][row]
    return out


def _inverse(matrix: list[list[float]]) -> list[list[float]] | None:
    n = len(matrix)
    cols = []
    for i in range(n):
        unit = [1.0 if j == i else 0.0 for j in range(n)]
        col = _solve(matrix, unit)
        if col is None:
            return None
        cols.append(col)
    return [[cols[j][i] for j in range(n)] for i in range(n)]


def _jacobi_eigenvalues(matrix: list[list[float]], sweeps: int = 60) -> list[float]:
    """Eigenvalues of a small symmetric matrix (cyclic Jacobi rotations)."""
    n = len(matrix)
    a = [row[:] for row in matrix]
    for _ in range(sweeps):
        off = sum(a[i][j] ** 2 for i in range(n) for j in range(n) if i != j)
        if off < 1e-18:
            break
        for p in range(n - 1):
            for q in range(p + 1, n):
                if abs(a[p][q]) < 1e-18:
                    continue
                theta = (a[q][q] - a[p][p]) / (2.0 * a[p][q])
                t = math.copysign(1.0, theta) / (abs(theta) + math.sqrt(theta * theta + 1.0))
                c = 1.0 / math.sqrt(t * t + 1.0)
                s = t * c
                for k in range(n):
                    akp, akq = a[k][p], a[k][q]
                    a[k][p] = c * akp - s * akq
                    a[k][q] = s * akp + c * akq
                for k in range(n):
                    apk, aqk = a[p][k], a[q][k]
                    a[p][k] = c * apk - s * aqk
                    a[q][k] = s * apk + c * aqk
    return sorted(a[i][i] for i in range(n))


@dataclass
class BatchFit:
    theta: list[float]
    stderr: list[float]
    layout: tuple[str, ...]
    n: int
    rss: float
    tss: float
    sigma: float
    vif: list[float]
    condition_number: float

    @property
    def r_squared(self) -> float:
        return 1.0 - self.rss / self.tss if self.tss > 0 else float("nan")


def batch_fit(
    samples: list[Sample], layout: tuple[str, ...], wind_ref: float
) -> BatchFit | None:
    """Ordinary least squares over all samples at once.

    This is the *upper bound* on what this model structure can do on this data
    — it sees everything, with no forgetting factor and no online transient. If
    the batch R^2 is poor, the problem is the model structure or the data, not
    the RLS tuning. Standard errors and VIFs come from the same fit, and are
    the honest answer to "is this parameter actually identified?".
    """
    n_params = len(layout)
    if len(samples) <= n_params + 1:
        return None

    rows = [design_row(s, layout, wind_ref) for s in samples]
    ys = [s.y for s in samples]
    n = len(rows)

    xtx = [
        [sum(rows[k][i] * rows[k][j] for k in range(n)) for j in range(n_params)]
        for i in range(n_params)
    ]
    xty = [sum(rows[k][i] * ys[k] for k in range(n)) for i in range(n_params)]
    theta = _solve(xtx, xty)
    if theta is None:
        return None

    residuals = [ys[k] - sum(rows[k][i] * theta[i] for i in range(n_params)) for k in range(n)]
    rss = sum(r * r for r in residuals)
    y_mean = statistics.fmean(ys)
    tss = sum((v - y_mean) ** 2 for v in ys)
    dof = max(n - n_params, 1)
    sigma_sq = rss / dof

    xtx_inv = _inverse(xtx)
    if xtx_inv is None:
        stderr = [float("nan")] * n_params
    else:
        stderr = [
            math.sqrt(sigma_sq * xtx_inv[i][i]) if xtx_inv[i][i] > 0 else float("nan")
            for i in range(n_params)
        ]

    # Condition number on column-normalised X'X, so it reflects genuine
    # collinearity rather than the arbitrary scaling of each regressor.
    norms = [math.sqrt(xtx[i][i]) if xtx[i][i] > 0 else 1.0 for i in range(n_params)]
    normalised = [
        [xtx[i][j] / (norms[i] * norms[j]) for j in range(n_params)] for i in range(n_params)
    ]
    eigs = _jacobi_eigenvalues(normalised)
    cond = (
        math.sqrt(abs(eigs[-1]) / abs(eigs[0]))
        if eigs and abs(eigs[0]) > 1e-15
        else float("inf")
    )

    # VIF per regressor: regress each column on the others.
    vif: list[float] = []
    for target in range(n_params):
        others = [i for i in range(n_params) if i != target]
        if not others:
            vif.append(1.0)
            continue
        col = [rows[k][target] for k in range(n)]
        col_mean = statistics.fmean(col)
        col_tss = sum((v - col_mean) ** 2 for v in col)
        sub_xtx = [[xtx[i][j] for j in others] for i in others]
        sub_xty = [sum(rows[k][i] * col[k] for k in range(n)) for i in others]
        coeffs = _solve(sub_xtx, sub_xty)
        if coeffs is None or col_tss <= 0:
            vif.append(float("inf"))
            continue
        col_rss = sum(
            (col[k] - sum(rows[k][others[i]] * coeffs[i] for i in range(len(others)))) ** 2
            for k in range(n)
        )
        r2 = 1.0 - col_rss / col_tss
        vif.append(1.0 / (1.0 - r2) if r2 < 0.999999 else float("inf"))

    return BatchFit(
        theta=theta,
        stderr=stderr,
        layout=layout,
        n=n,
        rss=rss,
        tss=tss,
        sigma=math.sqrt(sigma_sq),
        vif=vif,
        condition_number=cond,
    )


# --- error metrics -----------------------------------------------------------


@dataclass
class ErrorStats:
    n: int
    rmse: float
    mae: float
    bias: float

    @staticmethod
    def of(errors: list[float]) -> "ErrorStats":
        if not errors:
            return ErrorStats(0, float("nan"), float("nan"), float("nan"))
        return ErrorStats(
            n=len(errors),
            rmse=math.sqrt(statistics.fmean(e * e for e in errors)),
            mae=statistics.fmean(abs(e) for e in errors),
            bias=statistics.fmean(errors),
        )


def _skill(model: float, baseline: float) -> float:
    """Fraction of the baseline's error the model removes. <=0 means useless."""
    if not math.isfinite(model) or not math.isfinite(baseline) or baseline <= 0:
        return float("nan")
    return 1.0 - model / baseline


# --- report sections ---------------------------------------------------------


def report_data_quality(records: list[dict], samples: list[Sample], stats: dict) -> None:
    print("\n" + "=" * 78)
    print("1. DATA INVENTORY")
    print("=" * 78)

    if not records:
        print("  No records loaded.")
        return

    first, last = _parse_ts(records[0]["ts"]), _parse_ts(records[-1]["ts"])
    span_h = (last - first).total_seconds() / 3600.0 if first and last else 0.0
    print(f"  Records:           {stats['total']}")
    print(f"  Span:              {first} -> {last}  ({span_h:.1f} h = {span_h / 24:.1f} d)")
    print(f"  Usable transitions:{stats['usable']}")
    print(
        f"  Dropped:           {stats['no_indoor']} no-indoor, "
        f"{stats['non_finite']} non-finite, {stats['dt_out_of_range']} dt-gap, "
        f"{stats['implausible_step']} implausible-step"
    )
    if not samples:
        return

    dts = [s.dt_h * 60.0 for s in samples]
    print(f"  Cadence (min):     median {statistics.median(dts):.1f}, "
          f"min {min(dts):.1f}, max {max(dts):.1f}")

    def _rng(label: str, values: list[float], unit: str = "") -> None:
        print(
            f"  {label:<18} min {min(values):+.2f}{unit}, "
            f"median {statistics.median(values):+.2f}{unit}, "
            f"max {max(values):+.2f}{unit}"
        )

    _rng("Outdoor temp:", [s.outdoor_start for s in samples], " C")
    _rng("Indoor temp:", [s.indoor_start for s in samples], " C")
    _rng("Temp gap:", [s.temp_gap for s in samples], " C")
    _rng("Indoor step:", [s.y for s in samples], " C")
    _rng("Solar effect:", [s.solar for s in samples])
    _rng("Wind:", [s.wind_ms for s in samples], " m/s")

    active = sum(1 for s in samples if s.is_active)
    cutoff = sum(1 for s in samples if s.heating_cutoff)
    excited = [s for s in samples if abs(s.u) > rc_model.GAIN_EXCITATION_EPS]
    print(f"  Compensation on:   {active} / {len(samples)} ({100 * active / len(samples):.1f}%)")
    print(f"  Heating cutoff:    {cutoff} / {len(samples)} ({100 * cutoff / len(samples):.1f}%)")
    print(
        f"  Gain excitation:   {len(excited)} / {len(samples)} "
        f"({100 * len(excited) / len(samples):.1f}%) cycles with a nonzero applied delta"
    )
    if excited:
        mags = [abs(s.u) for s in excited]
        _rng("  applied |delta|:", mags, " C")
    else:
        print("    -> theta_gain CANNOT be identified from this data at all.")


def report_batch_fit(fit: BatchFit | None, title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)
    if fit is None:
        print("  Not enough data to fit.")
        return

    print(f"  Samples: {fit.n}   R^2: {fit.r_squared:.4f}   residual sigma: {fit.sigma:.4f} degC")
    print(f"  Condition number (normalised X'X): {fit.condition_number:.1f}")
    print()
    print(f"  {'param':<8}{'estimate':>13}{'std err':>12}{'t':>9}{'VIF':>9}  interpretation")
    for i, name in enumerate(fit.layout):
        est, se = fit.theta[i], fit.stderr[i]
        t = est / se if se and math.isfinite(se) and se > 0 else float("nan")
        note = ""
        if name == "env":
            tau = 1.0 / est if est > 0 else float("inf")
            note = f"tau = {tau:.1f} h"
            if est <= 0:
                note += "  <-- NON-PHYSICAL (negative envelope coupling)"
        elif name == "gain":
            note = "expect clearly NEGATIVE" if est >= 0 else "sign OK (negative)"
        elif name == "solar":
            note = "expect >= 0" + ("  <-- negative" if est < 0 else "")
        elif name == "wind":
            note = "expect >= 0" + ("  <-- negative" if est < 0 else "")
        if math.isfinite(t) and abs(t) < 2.0:
            note += "  [NOT significant, |t|<2]"
        print(f"  {name:<8}{est:>13.6f}{se:>12.6f}{t:>9.2f}{fit.vif[i]:>9.2f}  {note}")

    if fit.condition_number > 30:
        print("\n  WARNING: condition number > 30 — regressors are collinear on this")
        print("  data; individual parameters are poorly separated even if R^2 looks fine.")


def report_rls_replay(
    samples: list[Sample], enable_wind: bool, wind_ref: float
) -> rc_model.RCModelState:
    """Replay the live estimator over the whole history, from cold start."""
    print("\n" + "=" * 78)
    print("4. LIVE RLS REPLAY (what the running integration would have learned)")
    print("=" * 78)

    config = rc_model.RCModelConfig(enable_wind=enable_wind, wind_reference_ms=wind_ref)
    state = rc_model.initial_state(enable_wind=enable_wind)
    checkpoints = {int(len(samples) * f) for f in (0.1, 0.25, 0.5, 0.75, 1.0)}
    result: rc_model.RCModelResult | None = None

    # Feed the estimator the same (indoor, outdoor, u, solar, wind, dt) stream
    # the coordinator would have. Sample k carries the conditions at its start
    # and the indoor temperature at its end, so replaying in order reproduces
    # the live sequence exactly.
    for idx, sample in enumerate(samples, start=1):
        inputs = rc_model.RCModelInputs(
            indoor_temp_c=sample.indoor_start + sample.y,
            indoor_data_available=True,
            outdoor_temp_c=sample.outdoor_start,
            compensation_delta_c=sample.u,
            solar_effect=sample.solar,
            wind_speed_ms=sample.wind_ms,
            dt_seconds=sample.dt_h * 3600.0,
        )
        state, result = rc_model.step(state, inputs, config)
        if idx in checkpoints and result is not None:
            tau = result.time_constant_h
            gain = f"{result.theta_gain:+.4f}" if result.gain_modeled else "  n/a "
            print(
                f"  after {idx:>5} samples: tau={tau:>7.1f} h  gain={gain}  "
                f"solar={result.theta_solar:+.4f}  conf={result.confidence:.2f}"
            )

    if result is not None:
        print()
        print(f"  Final: accepted {result.accepted_samples}, rejected {result.rejected_samples}, "
              f"clip events {result.clip_events}")
        print(f"  Final tau: {result.time_constant_h:.1f} h   "
              f"theta_solar: {result.theta_solar:+.5f}   "
              f"theta_gain: "
              f"{f'{result.theta_gain:+.5f}' if result.gain_modeled else 'never modeled'}")
        if result.clip_events > 0:
            pct = 100 * result.clip_events / max(result.accepted_samples, 1)
            print(f"  NOTE: parameters hit a physical bound on {pct:.1f}% of accepted samples —")
            print("  the estimator is being held in place by clipping, not by the data.")
    return state


def report_holdout(
    samples: list[Sample], layout: tuple[str, ...], wind_ref: float, train_frac: float
) -> None:
    """Fit on the first chunk, freeze, predict the rest — vs persistence."""
    print("\n" + "=" * 78)
    print(f"5. ONE-STEP HOLDOUT ({train_frac:.0%} train / {1 - train_frac:.0%} test)")
    print("=" * 78)

    split = int(len(samples) * train_frac)
    train, test = samples[:split], samples[split:]
    if len(train) < len(layout) + 2 or not test:
        print("  Not enough data to split.")
        return

    fit = batch_fit(train, layout, wind_ref)
    if fit is None:
        print("  Training fit failed.")
        return

    model_err, persist_err = [], []
    for sample in test:
        phi = design_row(sample, layout, wind_ref)
        predicted = sum(fit.theta[i] * phi[i] for i in range(len(layout)))
        model_err.append(sample.y - predicted)
        persist_err.append(sample.y)  # persistence predicts dT = 0

    model, persist = ErrorStats.of(model_err), ErrorStats.of(persist_err)
    print(f"  Test samples: {model.n}")
    print(f"  {'':<14}{'RMSE':>10}{'MAE':>10}{'bias':>10}   (degC per step)")
    print(f"  {'RC model':<14}{model.rmse:>10.4f}{model.mae:>10.4f}{model.bias:>+10.4f}")
    print(f"  {'persistence':<14}{persist.rmse:>10.4f}{persist.mae:>10.4f}{persist.bias:>+10.4f}")
    skill = _skill(model.rmse, persist.rmse)
    print(f"\n  Skill vs persistence: {skill:+.1%} RMSE reduction")
    if skill <= 0:
        print("  VERDICT: the model is NO BETTER than predicting 'nothing changes'.")
    elif skill < 0.1:
        print("  VERDICT: marginal — under 10% better than a trivial baseline.")
    else:
        print("  VERDICT: the model carries real one-step information.")


def report_rollout(
    samples: list[Sample],
    layout: tuple[str, ...],
    wind_ref: float,
    train_frac: float,
    horizons_h: list[float],
) -> None:
    """Free-running multi-hour simulation — the regime MPC actually uses.

    Parameters are fitted on the training portion only, then the model is rolled
    out open-loop from many start points in the test portion, feeding it the
    *actual* outdoor/solar/wind/delta sequence (perfect forecasts). That isolates
    model error from forecast error: these numbers are the optimistic bound on
    what MPC could achieve, and MPC's real trajectories can only be worse.
    """
    print("\n" + "=" * 78)
    print("6. OPEN-LOOP ROLLOUT (multi-hour, perfect forecasts — the MPC regime)")
    print("=" * 78)

    split = int(len(samples) * train_frac)
    train, test = samples[:split], samples[split:]
    fit = batch_fit(train, layout, wind_ref)
    if fit is None or len(test) < 4:
        print("  Not enough data.")
        return

    idx = {name: i for i, name in enumerate(layout)}
    theta_env = fit.theta[idx["env"]]
    theta_solar = fit.theta[idx["solar"]]
    theta_gain = fit.theta[idx["gain"]] if "gain" in idx else 0.0
    theta_wind = fit.theta[idx["wind"]] if "wind" in idx else 0.0

    print(f"  {'horizon':>9}{'starts':>9}{'model RMSE':>13}{'persist RMSE':>14}{'skill':>9}")
    for horizon in horizons_h:
        model_err, persist_err = [], []
        for start in range(len(test)):
            t_in = test[start].indoor_start
            elapsed = 0.0
            end = start
            # Roll forward until the horizon is covered, breaking on any gap so
            # a restart never gets simulated straight through.
            while end < len(test) and elapsed < horizon:
                sample = test[end]
                if end > start:
                    gap = (sample.ts - test[end - 1].ts).total_seconds() / 3600.0
                    if gap > sample.dt_h * 2.5:
                        break
                gap_now = sample.outdoor_start - t_in
                delta = (
                    theta_env * gap_now
                    + theta_gain * sample.u
                    + theta_solar * sample.solar
                    + theta_wind * gap_now * (sample.wind_ms / wind_ref)
                ) * sample.dt_h
                t_in += delta
                elapsed += sample.dt_h
                end += 1
            if elapsed < horizon * 0.9 or end > len(test):
                continue
            actual = test[end - 1].indoor_start + test[end - 1].y
            model_err.append(actual - t_in)
            persist_err.append(actual - test[start].indoor_start)

        model, persist = ErrorStats.of(model_err), ErrorStats.of(persist_err)
        if model.n == 0:
            print(f"  {horizon:>8.1f}h{0:>9}   (no complete windows)")
            continue
        skill = _skill(model.rmse, persist.rmse)
        flag = "  <-- worse than persistence" if skill <= 0 else ""
        print(
            f"  {horizon:>8.1f}h{model.n:>9}{model.rmse:>13.3f}"
            f"{persist.rmse:>14.3f}{skill:>+9.1%}{flag}"
        )

    print("\n  Skill here is what matters for MPC: it plans over hours, not one step.")


def report_segments(samples: list[Sample], layout: tuple[str, ...], wind_ref: float) -> None:
    """Fit heating-season and cutoff/summer data separately.

    Summer venting (open windows) and heating-cutoff periods are known to
    contaminate the envelope estimate — a house with the windows open really is
    leakier, so the fit is not wrong, it is just answering a different question
    than "what is my closed-house time constant". Splitting them shows how much
    the headline tau depends on which regime dominates the data.
    """
    print("\n" + "=" * 78)
    print("7. REGIME SPLIT (is the envelope estimate regime-dependent?)")
    print("=" * 78)

    groups = {
        "heating (cutoff off)": [s for s in samples if not s.heating_cutoff],
        "cutoff engaged": [s for s in samples if s.heating_cutoff],
        "cold (T_out < 5C)": [s for s in samples if s.outdoor_start < 5.0],
        "mild (5-15C)": [s for s in samples if 5.0 <= s.outdoor_start < 15.0],
        "warm (>= 15C)": [s for s in samples if s.outdoor_start >= 15.0],
    }
    print(f"  {'segment':<24}{'n':>7}{'tau (h)':>11}{'solar':>11}{'R^2':>9}")
    for label, subset in groups.items():
        fit = batch_fit(subset, layout, wind_ref)
        if fit is None:
            print(f"  {label:<24}{len(subset):>7}      (too few samples)")
            continue
        idx = {name: i for i, name in enumerate(layout)}
        env = fit.theta[idx["env"]]
        tau = 1.0 / env if env > 0 else float("nan")
        print(
            f"  {label:<24}{fit.n:>7}{tau:>11.1f}"
            f"{fit.theta[idx['solar']]:>+11.4f}{fit.r_squared:>9.3f}"
        )
    print("\n  Widely differing tau across regimes = the 1R1C structure is not")
    print("  capturing something real (solar gain, occupancy, venting, or the")
    print("  indoor sensor not representing the conditioned space).")


# --- main --------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", type=Path, help=".jsonl / .jsonl.gz history files")
    parser.add_argument("--enable-wind", action="store_true")
    parser.add_argument("--wind-ref", type=float, default=rc_model.DEFAULT_WIND_REFERENCE_MS)
    parser.add_argument("--train-frac", type=float, default=0.7)
    parser.add_argument("--horizons", default="1,3,6,12")
    parser.add_argument("--min-outdoor", type=float, default=None)
    parser.add_argument("--exclude-cutoff", action="store_true")
    args = parser.parse_args()

    missing = [p for p in args.paths if not p.exists()]
    if missing:
        print(f"No such file(s): {', '.join(str(p) for p in missing)}", file=sys.stderr)
        return 1

    print(f"Loading {len(args.paths)} file(s)...")
    records = load_records(args.paths)
    samples, stats = build_samples(records)
    report_data_quality(records, samples, stats)

    if args.exclude_cutoff:
        samples = [s for s in samples if not s.heating_cutoff]
        print(f"\n  [filter] cutoff-engaged cycles dropped -> {len(samples)} samples")
    if args.min_outdoor is not None:
        samples = [s for s in samples if s.outdoor_start <= args.min_outdoor]
        print(f"  [filter] outdoor > {args.min_outdoor} C dropped -> {len(samples)} samples")

    if len(samples) < 30:
        print("\nToo few usable transitions to validate anything. Collect more data.")
        return 1

    # Only claim a gain dimension if the data ever actually excited it — the
    # same rule the live estimator uses, so the offline fit does not invent an
    # identifiable parameter the running system would never have added.
    has_gain = any(abs(s.u) > rc_model.GAIN_EXCITATION_EPS for s in samples)
    layout = rc_model._layout(args.enable_wind, has_gain)
    print(f"\n  Estimator layout: {layout}")
    if not has_gain:
        print("  (no gain dimension — zero applied-delta excitation in this data)")

    fit = batch_fit(samples, layout, args.wind_ref)
    report_batch_fit(fit, "2./3. BATCH FIT + IDENTIFIABILITY (best case for this structure)")
    report_rls_replay(samples, args.enable_wind, args.wind_ref)
    report_holdout(samples, layout, args.wind_ref, args.train_frac)
    horizons = [float(h) for h in args.horizons.split(",") if h.strip()]
    report_rollout(samples, layout, args.wind_ref, args.train_frac, horizons)
    report_segments(samples, layout, args.wind_ref)

    print("\n" + "=" * 78)
    print("Read sections 5 and 6 first. Convergence is not validation; beating")
    print("persistence over multi-hour horizons is.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
