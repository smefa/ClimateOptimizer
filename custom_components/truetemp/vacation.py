"""Multiple, recurring vacation plans: a list-of-records generalisation of
`holiday.py`'s single scenario.

Pure module: standard library only (`dataclasses`, `datetime`). It MAY
import from `holiday.py` (same package) — it reuses `HolidayResult`,
`resolve_window`, `HOLIDAY_TARGET_MIN_C`, `HOLIDAY_RETURN_TIME` and the
`HOLIDAY_PHASE_*` constants rather than redefining them — but nothing here
imports Home Assistant, so this stays importable and unit-testable without
it installed, same discipline as `holiday.py`/`lag.py`/`learner.py`. See
`docs/plan_vacation_plans.md` for the feature design this implements Phase 1
of.

## What's new here versus `holiday.py`

`holiday.py` resolves exactly one armed scenario with fixed `start_date`/
`end_date`. This module resolves a *list* of independently enabled
`VacationPlan` records, each with its own recurrence (`once`/`weekly`/
`yearly`), against a single master arm switch. The actual setback/ramp math
is unchanged — every plan still bottoms out in `holiday.resolve_window()`
once its concrete `(start_at, return_at)` occurrence window is picked.

## Priority, not lowest-temperature-wins

With N independently-enabled plans, more than one can be "in window" at
once. `resolve_vacation()` walks the plan list in order and the first plan
currently actively sagging the house (`setback`/`ramping`) wins outright —
deliberately not an automatic "whichever plan is colder wins" rule, since a
silent temperature-based tiebreak is the wrong failure mode for a heating
setback if the user's actual intent was the other plan. See §3 of the plan
doc.

## Never raises

Every public function here degrades malformed/missing plan fields to
`None`/inactive/invalid, the same philosophy `holiday.py`'s module docstring
states for its own inputs — a corrupt or half-filled-in plan must not be
allowed to crash the coordinator's update loop.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

# Package-relative import at runtime; the absolute fallback is only taken
# when this file is loaded standalone by file path in the test suite, which
# has no package context and pre-registers sibling modules in sys.modules —
# same shim as `learner_store.py`.
try:  # pragma: no cover - trivial import shim
    from .holiday import (
        HOLIDAY_PHASE_INACTIVE,
        HOLIDAY_PHASE_INVALID,
        HOLIDAY_PHASE_RAMPING,
        HOLIDAY_PHASE_SCHEDULED,
        HOLIDAY_PHASE_SETBACK,
        HOLIDAY_RETURN_TIME,
        HOLIDAY_TARGET_MIN_C,
        HolidayResult,
        resolve_window,
    )
except ImportError:  # pragma: no cover - test path-load fallback
    from holiday import (  # type: ignore[no-redef]
        HOLIDAY_PHASE_INACTIVE,
        HOLIDAY_PHASE_INVALID,
        HOLIDAY_PHASE_RAMPING,
        HOLIDAY_PHASE_SCHEDULED,
        HOLIDAY_PHASE_SETBACK,
        HOLIDAY_RETURN_TIME,
        HOLIDAY_TARGET_MIN_C,
        HolidayResult,
        resolve_window,
    )

_LOGGER = logging.getLogger(__name__)

RECURRENCE_ONCE = "once"
RECURRENCE_WEEKLY = "weekly"
RECURRENCE_YEARLY = "yearly"
RECURRENCES = (RECURRENCE_ONCE, RECURRENCE_WEEKLY, RECURRENCE_YEARLY)

# Phases that mean "actively sagging the house right now" — the priority
# tier `resolve_vacation()` picks a winner from first.
_ACTIVE_PHASES = (HOLIDAY_PHASE_SETBACK, HOLIDAY_PHASE_RAMPING)


@dataclass(frozen=True)
class VacationPlan:
    """One named vacation plan. `id` is a stable identifier (e.g. a uuid4)
    assigned on create and never reused, so the coordinator/sensor can track
    "which plan is active" across cycles even as plans are edited/reordered.

    Only the fields relevant to `recurrence` are populated; the rest are
    `None`. `occurrence_window()` treats a missing/malformed field for the
    plan's own recurrence kind as malformed (returns `None`), not a crash.
    """

    id: str
    name: str
    enabled: bool
    recurrence: str  # RECURRENCE_ONCE | RECURRENCE_WEEKLY | RECURRENCE_YEARLY
    min_temp_c: float
    # recurrence == "once"
    start_date: date | None = None
    end_date: date | None = None
    # recurrence == "weekly" — weekday ints match date.weekday() (Monday=0..Sunday=6)
    start_weekday: int | None = None
    start_time: time | None = None
    stop_weekday: int | None = None
    stop_time: time | None = None
    # recurrence == "yearly" — year-independent (month, day) pairs
    start_month: int | None = None
    start_day: int | None = None
    end_month: int | None = None
    end_day: int | None = None


def _once_window(plan: VacationPlan) -> tuple[datetime, datetime] | None:
    if plan.start_date is None or plan.end_date is None:
        return None
    if plan.end_date <= plan.start_date:
        return None
    start_at = datetime.combine(plan.start_date, time.min)
    return_at = datetime.combine(plan.end_date, HOLIDAY_RETURN_TIME)
    return start_at, return_at


def _weekly_stop_for_start(
    start_at: datetime, stop_weekday: int, stop_time: time
) -> datetime:
    """The paired stop datetime for a given start, per §3 step 2 of the
    occurrence algorithm: same-or-next matching (stop_weekday, stop_time)
    strictly after `start_at`, at most 7 days out."""
    offset_days = (stop_weekday - start_at.weekday()) % 7
    candidate = datetime.combine(start_at.date() + timedelta(days=offset_days), stop_time)
    if candidate <= start_at:
        candidate += timedelta(days=7)
    return candidate


def _weekly_fields_ok(plan: VacationPlan) -> bool:
    if (
        plan.start_weekday is None
        or plan.start_time is None
        or plan.stop_weekday is None
        or plan.stop_time is None
    ):
        return False
    return 0 <= plan.start_weekday <= 6 and 0 <= plan.stop_weekday <= 6


def _most_recent_weekly_start_on_or_before(
    anchor: datetime, weekday: int, t: time
) -> datetime:
    """The most recent datetime at-or-before `anchor` matching
    `(weekday, t)` — exactly one candidate within the 7 days up to and
    including `anchor`'s own day."""
    days_back = (anchor.weekday() - weekday) % 7
    candidate = datetime.combine(anchor.date() - timedelta(days=days_back), t)
    if candidate > anchor:
        candidate -= timedelta(days=7)
    return candidate


def _weekly_window(plan: VacationPlan, now: datetime) -> tuple[datetime, datetime] | None:
    if not _weekly_fields_ok(plan):
        return None

    # Step 1: most recent datetime on-or-before `now` matching
    # (start_weekday, start_time) — exactly one candidate within the past 7
    # days inclusive of today.
    recent_start = _most_recent_weekly_start_on_or_before(now, plan.start_weekday, plan.start_time)

    stop_for_recent_start = _weekly_stop_for_start(
        recent_start, plan.stop_weekday, plan.stop_time
    )

    if now < stop_for_recent_start:
        return recent_start, stop_for_recent_start

    next_start = recent_start + timedelta(days=7)
    stop_for_next_start = _weekly_stop_for_start(next_start, plan.stop_weekday, plan.stop_time)
    return next_start, stop_for_next_start


def _weekly_occurrences_in_range(
    plan: VacationPlan, lo: datetime, hi: datetime
) -> list[tuple[datetime, datetime]]:
    """Every occurrence of `plan` whose window could intersect `[lo, hi)` —
    bounded to roughly `(hi - lo) / 7 days + 1` candidates, proportional to
    the compared span, not open-ended.

    Starts from the most recent start at-or-before `lo` (catching a window
    already in progress at `lo`, since a weekly occurrence's own duration is
    always <= 7 days — anything that started earlier than that has already
    finished by `lo`), then steps forward one period (7 days) at a time
    until past `hi`.
    """
    if not _weekly_fields_ok(plan):
        return []
    start = _most_recent_weekly_start_on_or_before(lo, plan.start_weekday, plan.start_time)
    occurrences: list[tuple[datetime, datetime]] = []
    while start <= hi:
        stop = _weekly_stop_for_start(start, plan.stop_weekday, plan.stop_time)
        occurrences.append((start, stop))
        start += timedelta(days=7)
    return occurrences


def _yearly_window_for_start_year(
    plan: VacationPlan, start_year: int
) -> tuple[datetime, datetime] | None:
    try:
        start_at = datetime.combine(
            date(start_year, plan.start_month, plan.start_day), time.min
        )
    except ValueError:
        return None
    end_year = start_year
    # Wrap case: end (month, day) earlier in the calendar than start (month,
    # day) means the trip spans New Year's and ends the following year.
    if (plan.end_month, plan.end_day) <= (plan.start_month, plan.start_day):
        end_year += 1
    try:
        return_at = datetime.combine(
            date(end_year, plan.end_month, plan.end_day), HOLIDAY_RETURN_TIME
        )
    except ValueError:
        return None
    if return_at <= start_at:
        return None
    return start_at, return_at


def _yearly_fields_ok(plan: VacationPlan) -> bool:
    return not (
        plan.start_month is None
        or plan.start_day is None
        or plan.end_month is None
        or plan.end_day is None
    )


def _yearly_window(plan: VacationPlan, now: datetime) -> tuple[datetime, datetime] | None:
    if not _yearly_fields_ok(plan):
        return None

    candidates = []
    for start_year in (now.year - 1, now.year, now.year + 1):
        window = _yearly_window_for_start_year(plan, start_year)
        if window is not None:
            candidates.append(window)
    if not candidates:
        return None

    # Currently-active occurrence wins if one exists.
    for start_at, return_at in candidates:
        if start_at <= now < return_at:
            return start_at, return_at

    # Else nearest upcoming.
    upcoming = [w for w in candidates if w[0] > now]
    if upcoming:
        return min(upcoming, key=lambda w: w[0])

    # All candidates are in the past (shouldn't normally happen given the
    # now.year + 1 candidate, but degrade gracefully rather than raising).
    return max(candidates, key=lambda w: w[0])


def _yearly_occurrences_in_range(
    plan: VacationPlan, lo: datetime, hi: datetime
) -> list[tuple[datetime, datetime]]:
    """Every occurrence of `plan` whose window could intersect `[lo, hi)` —
    bounded to roughly `(hi - lo) / 365 days + 2` candidates: one calendar
    year per iteration from one year before `lo` (catching a window already
    in progress at `lo`, e.g. a New Year's-wrapping trip that started the
    prior December) through one year past `hi`.
    """
    if not _yearly_fields_ok(plan):
        return []
    occurrences: list[tuple[datetime, datetime]] = []
    for start_year in range(lo.year - 1, hi.year + 2):
        window = _yearly_window_for_start_year(plan, start_year)
        if window is None:
            continue
        start_at, _return_at = window
        if start_at > hi:
            break
        occurrences.append(window)
    return occurrences


def occurrence_window(plan: VacationPlan, now: datetime) -> tuple[datetime, datetime] | None:
    """The occurrence of `plan`'s recurrence currently containing `now`, else
    the next upcoming one. `None` if the plan's fields for its own
    `recurrence` are missing/malformed — never raises."""
    if plan.recurrence == RECURRENCE_ONCE:
        return _once_window(plan)
    if plan.recurrence == RECURRENCE_WEEKLY:
        return _weekly_window(plan, now)
    if plan.recurrence == RECURRENCE_YEARLY:
        return _yearly_window(plan, now)
    return None


def resolve_plan(
    now: datetime,
    plan: VacationPlan,
    normal_target_c: float,
    rise_hours: float,
) -> HolidayResult:
    """Resolve one plan's phase/target for this cycle. Never raises: a
    disabled plan or one whose recurrence fields don't resolve to a window
    degrades to inactive/invalid, same as `holiday.resolve()`."""
    if not plan.enabled:
        return HolidayResult(
            phase=HOLIDAY_PHASE_INACTIVE,
            target_c=normal_target_c,
            start_at=None,
            ramp_start_at=None,
            return_at=None,
            hours_needed=0.0,
            on_track=True,
            reason=f"Plan {plan.name!r} is disabled",
        )

    window = occurrence_window(plan, now)
    if window is None:
        return HolidayResult(
            phase=HOLIDAY_PHASE_INVALID,
            target_c=normal_target_c,
            start_at=None,
            ramp_start_at=None,
            return_at=None,
            hours_needed=0.0,
            on_track=True,
            reason=(
                f"Plan {plan.name!r} has malformed/missing fields for its "
                f"{plan.recurrence!r} recurrence"
            ),
        )

    start_at, return_at = window
    holiday_target_c = max(plan.min_temp_c, HOLIDAY_TARGET_MIN_C)
    return resolve_window(
        now=now,
        start_at=start_at,
        return_at=return_at,
        normal_target_c=normal_target_c,
        holiday_target_c=holiday_target_c,
        rise_hours=rise_hours,
    )


def _inactive_vacation(normal_target_c: float, reason: str) -> HolidayResult:
    return HolidayResult(
        phase=HOLIDAY_PHASE_INACTIVE,
        target_c=normal_target_c,
        start_at=None,
        ramp_start_at=None,
        return_at=None,
        hours_needed=0.0,
        on_track=True,
        reason=reason,
    )


def resolve_vacation(
    now: datetime,
    master_armed: bool,
    plans: list[VacationPlan],
    normal_target_c: float,
    rise_hours: float,
) -> tuple[HolidayResult, str | None]:
    """Resolve the whole plan list for this cycle against the master arm
    switch. Returns `(result, active_plan_id)` — `active_plan_id` is `None`
    whenever no plan is active or scheduled.

    Priority (§3 of the plan doc, explicit list-order, not lowest-temp-wins):
    1. The FIRST plan (list order) currently `setback`/`ramping` wins outright.
    2. Else, if at least one plan is `scheduled`, the one with the SOONEST
       `start_at` wins (ties broken by list order) — a countdown for "starts
       soonest" is more useful to surface than "listed first".
    3. Else (all inactive/invalid/done, or an empty list): inactive.
    """
    if not master_armed:
        return _inactive_vacation(normal_target_c, "Vacation mode not armed"), None

    resolved = [(plan, resolve_plan(now, plan, normal_target_c, rise_hours)) for plan in plans]

    for plan, result in resolved:
        if result.phase in _ACTIVE_PHASES:
            return result, plan.id

    scheduled = [
        (plan, result) for plan, result in resolved if result.phase == HOLIDAY_PHASE_SCHEDULED
    ]
    if scheduled:
        plan, result = min(scheduled, key=lambda pr: pr[1].start_at)
        return result, plan.id

    return _inactive_vacation(normal_target_c, "No vacation plan active"), None


def _occurrences_in_range(
    plan: VacationPlan, lo: datetime, hi: datetime
) -> list[tuple[datetime, datetime]]:
    """Every occurrence of `plan` (of any recurrence kind) whose window
    could intersect `[lo, hi)` — the range-generation counterpart to
    `occurrence_window()`'s single nearest-to-`now` pick. A `once` plan has
    exactly one occurrence regardless of `lo`/`hi`."""
    if plan.recurrence == RECURRENCE_ONCE:
        window = _once_window(plan)
        return [window] if window is not None else []
    if plan.recurrence == RECURRENCE_WEEKLY:
        return _weekly_occurrences_in_range(plan, lo, hi)
    if plan.recurrence == RECURRENCE_YEARLY:
        return _yearly_occurrences_in_range(plan, lo, hi)
    return []


def _windows_intersect(
    a: tuple[datetime, datetime], b: tuple[datetime, datetime]
) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _plans_overlap(plan_a: VacationPlan, plan_b: VacationPlan, now: datetime) -> bool:
    """Whether `plan_a` and `plan_b` have ANY occurrence windows that
    intersect — not just whichever pair happens to be nearest `now`.

    Two cases:

    - Same recurrence kind (including both `once`, which only ever has one
      occurrence anyway): compare only the pair of windows nearest `now`
      via `occurrence_window()`. `once` windows are fixed regardless of
      `now`, so that IS the only window. `weekly`-vs-`weekly` and
      `yearly`-vs-`yearly` recur with an IDENTICAL period for both plans,
      so the pair nearest `now` is a time-shifted copy of every other pair:
      if they overlap, every period's pair does; if they don't, none do.
      No range generation needed.

    - Mixed kinds (`once`-vs-`weekly`, `once`-vs-`yearly`,
      `weekly`-vs-`yearly`): the two plans do NOT share a common period, so
      the nearest-to-`now` shortcut is unsound — e.g. a `once` plan three
      months out can collide with a `weekly` plan's occurrence three months
      out even though this week's occurrence doesn't, and a `once` plan
      years out can land inside some future year's `yearly` occurrence even
      though neither plan's *nearest* occurrence overlaps. One plan
      supplies a single concrete reference window and the other's
      occurrences are generated across that window's span — bounded,
      proportional to the span, via `_occurrences_in_range()` — and each is
      checked individually.

      Priority for which plan anchors: a `once` plan's window is fixed
      independent of `now`, however far away, so it always anchors when
      present. For `weekly`-vs-`yearly` (neither fixed), the yearly plan
      anchors — the less frequent recurrence — and the weekly plan's
      occurrences are searched across that one yearly window's span. This
      is the practically useful check for save-time validation; it does not
      exhaustively prove no *other* year's yearly occurrence could ever
      collide with the weekly plan (that would require re-anchoring on
      every future yearly occurrence, since weekday alignment drifts by a
      day or two each calendar year), but that's out of scope for a
      near-term "does this conflict" check.
    """
    if plan_a.recurrence == plan_b.recurrence:
        window_a = occurrence_window(plan_a, now)
        window_b = occurrence_window(plan_b, now)
        if window_a is None or window_b is None:
            return False
        return _windows_intersect(window_a, window_b)

    if plan_a.recurrence == RECURRENCE_ONCE:
        anchor_plan, other_plan = plan_a, plan_b
    elif plan_b.recurrence == RECURRENCE_ONCE:
        anchor_plan, other_plan = plan_b, plan_a
    elif plan_a.recurrence == RECURRENCE_YEARLY:
        anchor_plan, other_plan = plan_a, plan_b
    else:
        anchor_plan, other_plan = plan_b, plan_a

    anchor_window = occurrence_window(anchor_plan, now)
    if anchor_window is None:
        return False
    lo, hi = anchor_window
    for occurrence in _occurrences_in_range(other_plan, lo, hi):
        if _windows_intersect(anchor_window, occurrence):
            return True
    return False


def find_overlap(plans: list[VacationPlan], now: datetime) -> tuple[str, str] | None:
    """The `(id, id)` pair of the first two ENABLED plans (list order) whose
    occurrence windows overlap at any point — not just their occurrence
    nearest `now`, see `_plans_overlap()` — or `None`. Plans whose
    `occurrence_window()` is `None` (invalid) are skipped, not compared.
    Pairwise — fine for the small plan lists this feature expects, no
    interval-tree needed."""
    candidates = [
        plan
        for plan in plans
        if plan.enabled and occurrence_window(plan, now) is not None
    ]

    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            if _plans_overlap(candidates[i], candidates[j], now):
                return candidates[i].id, candidates[j].id
    return None


# --- Persistence -------------------------------------------------------
#
# The plan list lives in the config entry's `options` (Progress note in
# docs/plan_vacation_plans.md — not a separate `Store`; a user-authored list
# edited through the options flow has none of the "must survive an options
# reload" conflict that kept the old single holiday scenario out of options).
# These functions only convert to/from JSON-safe dicts; the actual read/write
# of `entry.options` happens in `config_flow.py`.
#
# `deserialize_plan`/`deserialize_plans` never raise and follow
# `learner_store.py`'s discipline: a corrupt individual plan is dropped, not
# allowed to discard the rest of the list.


def _parse_date(raw: Any) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _parse_time(raw: Any) -> time | None:
    if not isinstance(raw, str):
        return None
    try:
        return time.fromisoformat(raw)
    except ValueError:
        return None


def serialize_plan(plan: VacationPlan) -> dict[str, Any]:
    """`VacationPlan` -> a JSON-safe dict. Every date/time field serializes
    to its ISO string; a field that is `None` (not relevant to this plan's
    `recurrence`) serializes as `None`."""
    return {
        "id": plan.id,
        "name": plan.name,
        "enabled": plan.enabled,
        "recurrence": plan.recurrence,
        "min_temp_c": plan.min_temp_c,
        "start_date": plan.start_date.isoformat() if plan.start_date else None,
        "end_date": plan.end_date.isoformat() if plan.end_date else None,
        "start_weekday": plan.start_weekday,
        "start_time": plan.start_time.isoformat() if plan.start_time else None,
        "stop_weekday": plan.stop_weekday,
        "stop_time": plan.stop_time.isoformat() if plan.stop_time else None,
        "start_month": plan.start_month,
        "start_day": plan.start_day,
        "end_month": plan.end_month,
        "end_day": plan.end_day,
    }


def deserialize_plan(raw: Any) -> VacationPlan | None:
    """One stored plan dict -> `VacationPlan`, or `None` if it is not usable
    as one. Never raises."""
    if not isinstance(raw, dict):
        return None
    try:
        plan_id = raw["id"]
        name = raw["name"]
        recurrence = raw["recurrence"]
        if not isinstance(plan_id, str) or not plan_id:
            raise ValueError("missing/blank id")
        if not isinstance(name, str):
            raise TypeError("name is not a string")
        if recurrence not in RECURRENCES:
            raise ValueError(f"unknown recurrence {recurrence!r}")
        raw_enabled = raw.get("enabled", False)
        enabled = raw_enabled if isinstance(raw_enabled, bool) else False
        min_temp_c = float(raw["min_temp_c"])
        if not math.isfinite(min_temp_c):
            raise ValueError("non-finite min_temp_c")
    except (KeyError, TypeError, ValueError):
        return None

    # Int-typed fields: type-checked here (a wrong type makes the whole plan
    # untrustworthy), range-checked downstream by `occurrence_window()`'s own
    # `_weekly_fields_ok`/`_yearly_fields_ok` guards, which already treat an
    # out-of-range value as "malformed" rather than raising.
    int_fields = {
        "start_weekday": raw.get("start_weekday"),
        "stop_weekday": raw.get("stop_weekday"),
        "start_month": raw.get("start_month"),
        "start_day": raw.get("start_day"),
        "end_month": raw.get("end_month"),
        "end_day": raw.get("end_day"),
    }
    for value in int_fields.values():
        if value is not None and not isinstance(value, int):
            return None

    return VacationPlan(
        id=plan_id,
        name=name,
        enabled=enabled,
        recurrence=recurrence,
        min_temp_c=min_temp_c,
        start_date=_parse_date(raw.get("start_date")),
        end_date=_parse_date(raw.get("end_date")),
        start_time=_parse_time(raw.get("start_time")),
        stop_time=_parse_time(raw.get("stop_time")),
        **int_fields,
    )


def serialize_plans(plans: list[VacationPlan]) -> list[dict[str, Any]]:
    return [serialize_plan(plan) for plan in plans]


def deserialize_plans(raw: Any) -> list[VacationPlan]:
    """A stored plan list -> `list[VacationPlan]`, dropping any individual
    corrupt entry rather than discarding the whole list. `raw` that isn't
    even a list (missing key, corrupt options blob) degrades to `[]`."""
    if not isinstance(raw, list):
        return []
    plans: list[VacationPlan] = []
    for item in raw:
        plan = deserialize_plan(item)
        if plan is None:
            _LOGGER.debug("Vacation plan discarded: corrupt payload (%r)", item)
            continue
        plans.append(plan)
    return plans
