"""Unit tests for the holiday setback module.

`resolve()` is pure and stateless — every case here is a synthetic scenario
with a known right answer, same discipline as `test_lag.py`. What this file
does NOT cover is whether the coordinator actually threads the resulting
target through to the learner/lag estimator/price logic — that's
`test_control_loop.py`'s `TestHolidaySetback`, which drives the real modules
together and is the regression guard for the wiring, not the math.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from loader import load  # noqa: E402

holiday = load("holiday")

resolve = holiday.resolve


class TestInactive:
    def test_unarmed_passes_the_normal_target_through_unchanged(self):
        result = resolve(
            now=datetime(2026, 1, 5, 12, 0),
            armed=False,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 10),
            normal_target_c=21.0,
            holiday_target_c=17.0,
            rise_hours=1.5,
        )
        assert result.phase == holiday.HOLIDAY_PHASE_INACTIVE
        assert result.target_c == 21.0
        assert result.on_track is True

    def test_armed_with_no_dates_is_inactive(self):
        for start, end in ((None, date(2026, 1, 10)), (date(2026, 1, 1), None), (None, None)):
            result = resolve(
                now=datetime(2026, 1, 5, 12, 0),
                armed=True,
                start_date=start,
                end_date=end,
                normal_target_c=21.0,
                holiday_target_c=17.0,
                rise_hours=1.5,
            )
            assert result.phase == holiday.HOLIDAY_PHASE_INACTIVE
            assert result.target_c == 21.0

    def test_end_not_after_start_is_invalid_not_a_crash(self):
        same_day = resolve(
            now=datetime(2026, 1, 5, 12, 0),
            armed=True,
            start_date=date(2026, 1, 5),
            end_date=date(2026, 1, 5),
            normal_target_c=21.0,
            holiday_target_c=17.0,
            rise_hours=1.5,
        )
        assert same_day.phase == holiday.HOLIDAY_PHASE_INVALID
        assert same_day.target_c == 21.0
        assert same_day.start_at is None

        backwards = resolve(
            now=datetime(2026, 1, 5, 12, 0),
            armed=True,
            start_date=date(2026, 1, 10),
            end_date=date(2026, 1, 5),
            normal_target_c=21.0,
            holiday_target_c=17.0,
            rise_hours=1.5,
        )
        assert backwards.phase == holiday.HOLIDAY_PHASE_INVALID
        assert backwards.target_c == 21.0


class TestScheduledAndSetback:
    def test_before_the_start_date_is_scheduled_at_the_normal_target(self):
        result = resolve(
            now=datetime(2026, 1, 1, 0, 0),
            armed=True,
            start_date=date(2026, 1, 5),
            end_date=date(2026, 1, 10),
            normal_target_c=21.0,
            holiday_target_c=17.0,
            rise_hours=1.5,
        )
        assert result.phase == holiday.HOLIDAY_PHASE_SCHEDULED
        assert result.target_c == 21.0

    def test_setback_holds_exactly_at_the_holiday_target_across_the_plateau(self):
        # Huge runway (5 days) relative to the ramp this delta/rise_hours
        # needs (delta=4, rise_hours=1 -> 8h, see RAMP_HOURS_PER_DEGREE_MULTIPLE),
        # so there is a real plateau to sample across.
        start_date = date(2026, 1, 5)
        end_date = date(2026, 1, 10)
        kwargs = dict(
            armed=True,
            start_date=start_date,
            end_date=end_date,
            normal_target_c=21.0,
            holiday_target_c=17.0,
            rise_hours=1.0,
        )
        start_at = datetime.combine(start_date, time.min)
        for hours_in in (0.0, 6.0, 24.0, 72.0, 120.0):
            result = resolve(now=start_at + timedelta(hours=hours_in), **kwargs)
            assert result.phase == holiday.HOLIDAY_PHASE_SETBACK, hours_in
            assert result.target_c == 17.0, hours_in
            assert result.on_track is True
            # start_at is the fixed instant the setback stepped down — it
            # never moves within a cycle, unlike ramp_start_at which tracks
            # the *return* ramp near the other end of the trip.
            assert result.start_at == start_at, hours_in


class TestRamp:
    def test_the_ramp_is_monotonic_and_reaches_the_normal_target_by_return_at(self):
        start_date = date(2026, 1, 5)
        end_date = date(2026, 1, 10)
        kwargs = dict(
            armed=True,
            start_date=start_date,
            end_date=end_date,
            normal_target_c=21.0,
            holiday_target_c=17.0,
            rise_hours=1.0,
        )
        probe = resolve(now=datetime.combine(start_date, holiday.HOLIDAY_RETURN_TIME), **kwargs)
        ramp_start_at = probe.ramp_start_at
        return_at = probe.return_at
        assert ramp_start_at is not None and return_at is not None
        window = (return_at - ramp_start_at).total_seconds()
        assert window > 0
        # start_at (the setback's step-down instant, at the leave date) is
        # distinct from ramp_start_at (the return ramp's start, near the
        # return date) whenever there's a real plateau between them.
        assert probe.start_at == datetime.combine(start_date, time.min)
        assert probe.start_at != ramp_start_at

        samples = [
            resolve(now=ramp_start_at + timedelta(seconds=window * frac), **kwargs)
            for frac in (0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 0.99)
        ]
        assert all(r.phase == holiday.HOLIDAY_PHASE_RAMPING for r in samples)
        targets = [r.target_c for r in samples]
        assert targets == sorted(targets)
        assert targets[0] == 17.0
        assert targets[-1] > targets[0]
        assert all(t <= 21.0 for t in targets)

        at_return = resolve(now=return_at, **kwargs)
        assert at_return.phase == holiday.HOLIDAY_PHASE_DONE
        assert at_return.target_c == 21.0

    def test_no_ramp_needed_when_the_holiday_target_is_not_actually_colder(self):
        """A holiday target at or above the normal target is a no-op setback
        — nothing to ramp back from, so this must never divide by a zero or
        negative delta."""
        start_date = date(2026, 1, 5)
        end_date = date(2026, 1, 6)
        result = resolve(
            now=datetime.combine(start_date, holiday.HOLIDAY_RETURN_TIME),
            armed=True,
            start_date=start_date,
            end_date=end_date,
            normal_target_c=18.0,
            holiday_target_c=21.0,
            rise_hours=1.5,
        )
        assert result.phase == holiday.HOLIDAY_PHASE_SETBACK
        assert result.target_c == 21.0
        assert result.hours_needed == holiday.MIN_RAMP_HOURS


class TestInsufficientRunway:
    def test_ramp_starts_immediately_at_the_start_date_when_the_window_is_too_short(self):
        # delta=11, rise_hours=2 -> hours_needed = 44h (RAMP_HOURS_PER_DEGREE_MULTIPLE=2),
        # but the two dates only leave 39h of runway (Jan5 00:00 -> Jan6 15:00).
        start_date = date(2026, 1, 5)
        end_date = date(2026, 1, 6)
        start_at = datetime.combine(start_date, time.min)
        kwargs = dict(
            armed=True,
            start_date=start_date,
            end_date=end_date,
            normal_target_c=21.0,
            holiday_target_c=10.0,
            rise_hours=2.0,
        )

        at_start = resolve(now=start_at, **kwargs)
        assert at_start.on_track is False
        # Ramping from the very first instant, not a flat plateau first.
        assert at_start.phase == holiday.HOLIDAY_PHASE_RAMPING
        assert at_start.target_c == 10.0
        assert at_start.ramp_start_at == start_at
        # No plateau at all here, so start_at and ramp_start_at coincide.
        assert at_start.start_at == start_at

        # And it still reaches the normal target by the return deadline —
        # late relative to the requested ramp size, but not unbounded.
        at_return = resolve(now=at_start.return_at, **kwargs)
        assert at_return.phase == holiday.HOLIDAY_PHASE_DONE
        assert at_return.target_c == 21.0


class TestComfortFloorContract:
    def test_the_callers_clamped_value_is_respected_verbatim(self):
        """holiday.py has no notion of comfort_min_c — clamping is the
        caller's job (see resolve()'s docstring, and coordinator.py's
        `max(self.holiday_target_c, self.comfort_min_c)`). This is the
        guarantee that makes that division of responsibility safe: whatever
        already-clamped value the caller hands in comes back out verbatim as
        `target_c` during the plateau, never silently re-derived here."""
        already_clamped = 18.0
        result = resolve(
            now=datetime(2026, 1, 6, 0, 0),
            armed=True,
            start_date=date(2026, 1, 5),
            end_date=date(2026, 1, 20),
            normal_target_c=21.0,
            holiday_target_c=already_clamped,
            rise_hours=1.5,
        )
        assert result.phase == holiday.HOLIDAY_PHASE_SETBACK
        assert result.target_c == already_clamped


class TestTimezoneAwareCallerContract:
    def test_a_timezone_aware_now_raises_unless_stripped_first(self):
        """Regression guard for the bug found while wiring this into
        coordinator.py: `dt_util.now()` is timezone-aware in real Home
        Assistant, but `start_at`/`ramp_start_at`/`return_at` here are all
        naive (`datetime.combine(date, time)`), and Python raises `TypeError`
        comparing a naive and an aware datetime. The coordinator strips
        tzinfo before calling in
        (`dt_util.now().replace(tzinfo=None)`) — this pins that the naive
        form works and the aware form is exactly what breaks, so nobody
        "fixes" the strip back out later thinking it was defensive
        boilerplate.
        """
        import datetime as dt

        aware_now = datetime(2026, 1, 7, 12, 0, tzinfo=dt.timezone.utc)
        kwargs = dict(
            armed=True,
            start_date=date(2026, 1, 5),
            end_date=date(2026, 1, 10),
            normal_target_c=21.0,
            holiday_target_c=17.0,
            rise_hours=1.5,
        )
        try:
            resolve(now=aware_now, **kwargs)
        except TypeError:
            pass
        else:
            raise AssertionError(
                "expected TypeError comparing an aware `now` against holiday.py's "
                "naive datetimes — if this stopped raising, double-check whether "
                "the coordinator's `dt_util.now().replace(tzinfo=None)` strip is "
                "still needed before removing it"
            )

        result = resolve(now=aware_now.replace(tzinfo=None), **kwargs)
        assert result.phase in holiday.HOLIDAY_PHASES


class TestNeverRaises:
    def test_naive_datetimes_throughout_do_not_raise(self):
        """`now`/`start_date`/`end_date` are all naive by contract (see
        resolve()'s docstring) — this just pins that nothing here secretly
        expects timezone-aware input."""
        for phase_now in (
            datetime(2026, 1, 1, 0, 0),
            datetime(2026, 1, 5, 0, 0),
            datetime(2026, 1, 7, 12, 0),
            datetime(2026, 1, 10, 15, 0),
            datetime(2026, 1, 15, 0, 0),
        ):
            result = resolve(
                now=phase_now,
                armed=True,
                start_date=date(2026, 1, 5),
                end_date=date(2026, 1, 10),
                normal_target_c=21.0,
                holiday_target_c=17.0,
                rise_hours=1.5,
            )
            assert result.phase in holiday.HOLIDAY_PHASES
