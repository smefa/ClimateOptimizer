"""Unit tests for the response-time estimator.

The load-bearing test is `test_recovers_known_asymmetric_lags`: it builds a
synthetic house whose rise and fall delays are known and different, and checks
both are recovered. That is the same discipline that made the previous model's
negative result trustworthy — a harness self-tested against data with known
answers before it is pointed at real data.

`test_offset_events_during_a_setpoint_transient_are_discarded` guards the
finding that forced this module's rewrite: in a closed loop the controller
output and the plant output are coupled, so anything that reads them as cause
and effect without checking what else was moving will measure nonsense.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from loader import load  # noqa: E402

lag = load("lag")

STEP_MINUTES = 15.0


def make_series(
    rise_steps: int, fall_steps: int, cycles: int = 20, half_period: int = 15
) -> tuple[list[float], list[float]]:
    """A square-wave offset and the indoor response of a pure-delay house.

    Down-steps of the offset (spoofing colder, so asking for more heat) show up
    as indoor rising `rise_steps` later; up-steps show up as indoor falling
    `fall_steps` later. Deliberately asymmetric, because a real house's slab
    keeps delivering long after the pump backs off.
    """
    n = cycles * 2 * half_period
    offsets = [0.0 if (i // half_period) % 2 == 0 else -1.0 for i in range(n)]
    indoors = [20.0]
    for i in range(1, n):
        delta = 0.0
        src = i - rise_steps
        if src >= 1:
            move = offsets[src] - offsets[src - 1]
            if move < 0:
                delta += -move * 0.5
        src = i - fall_steps
        if src >= 1:
            move = offsets[src] - offsets[src - 1]
            if move > 0:
                delta += -move * 0.5
        indoors.append(indoors[-1] + delta)
    return offsets, indoors


def build_state(
    offsets: list[float], indoors: list[float], targets: list[float] | None = None
) -> "lag.LagState":
    """Default to a target that never moves, so offset events are readable."""
    if targets is None:
        targets = [21.0] * len(offsets)
    state = lag.initial_state(STEP_MINUTES)
    for offset, indoor, target in zip(offsets, indoors, targets):
        state = lag.push(state, offset, indoor, target)
    return state


class TestFallbacks:
    def test_each_heating_type_has_a_distinct_pair(self):
        radiators = lag.fallback_minutes("radiators")
        underfloor = lag.fallback_minutes("underfloor")
        mixed = lag.fallback_minutes("mixed")
        assert radiators[0] < mixed[0] < underfloor[0]
        assert radiators[1] < mixed[1] < underfloor[1]

    def test_fall_is_never_shorter_than_rise(self):
        """A house cannot stop gaining heat faster than it starts."""
        for heating_type in lag.HEATING_TYPES:
            rise, fall = lag.fallback_minutes(heating_type)
            assert fall >= rise

    def test_unknown_type_falls_back_to_the_default(self):
        assert lag.fallback_minutes("geothermal wizardry") == lag.fallback_minutes(
            lag.DEFAULT_HEATING_TYPE
        )
        assert lag.fallback_minutes(None) == lag.fallback_minutes(
            lag.DEFAULT_HEATING_TYPE
        )


class TestBuffer:
    def test_push_appends_every_series(self):
        state = lag.push(lag.initial_state(STEP_MINUTES), -1.0, 20.0, 21.0)
        assert state.offsets_c == (-1.0,)
        assert state.indoors_c == (20.0,)
        assert state.targets_c == (21.0,)

    def test_buffer_is_bounded(self):
        state = lag.initial_state(STEP_MINUTES)
        for i in range(lag.BUFFER_SAMPLES + 50):
            state = lag.push(state, -1.0, 20.0 + i, 21.0)
        assert len(state.offsets_c) == lag.BUFFER_SAMPLES
        assert len(state.indoors_c) == lag.BUFFER_SAMPLES
        # The newest samples survive, not the oldest.
        assert state.indoors_c[-1] == 20.0 + lag.BUFFER_SAMPLES + 49

    def test_missing_indoor_reading_clears_the_buffer(self):
        """A gap must not be stitched across: the discontinuity would register
        as an enormous fake movement event at whatever lag lined up."""
        state = build_state(*make_series(4, 8, cycles=2))
        assert state.indoors_c
        state = lag.push(state, -1.0, None, 21.0)
        assert state.offsets_c == ()
        assert state.indoors_c == ()
        assert state.targets_c == ()


class TestEstimate:
    def test_insufficient_history_reports_unmeasured(self):
        state = lag.initial_state(STEP_MINUTES)
        for i in range(10):
            state = lag.push(state, -1.0, 20.0 + i * 0.1, 21.0)
        result = lag.estimate(state, "radiators")
        assert not result.rise_measured
        assert not result.fall_measured
        assert (result.rise_minutes, result.fall_minutes) == lag.fallback_minutes(
            "radiators"
        )
        assert "Building history" in result.reason

    def test_recovers_known_asymmetric_lags(self):
        rise_steps, fall_steps = 4, 8
        state = build_state(*make_series(rise_steps, fall_steps))
        result = lag.estimate(state, "radiators")

        assert result.rise_measured
        assert result.fall_measured
        assert result.rise_minutes == rise_steps * STEP_MINUTES
        assert result.fall_minutes == fall_steps * STEP_MINUTES
        # The asymmetry is the point — a single lag number would be wrong for
        # one of the two directions.
        assert result.fall_minutes > result.rise_minutes

    def test_recovers_a_symmetric_lag_too(self):
        state = build_state(*make_series(6, 6))
        result = lag.estimate(state, "underfloor")
        assert result.rise_minutes == 6 * STEP_MINUTES
        assert result.fall_minutes == 6 * STEP_MINUTES

    def test_flat_data_reports_unmeasured(self):
        """A house that never moves has no lag to find, and saying so is the
        whole point of the confidence gate."""
        n = 400
        state = build_state([0.0] * n, [20.0] * n)
        result = lag.estimate(state, "underfloor")
        assert not result.rise_measured
        assert not result.fall_measured
        assert (result.rise_minutes, result.fall_minutes) == lag.fallback_minutes(
            "underfloor"
        )

    def test_offset_moves_with_no_response_report_unmeasured(self):
        """Offset swinging while indoor sits still is a house that is not
        responding — not a house with a lag of zero."""
        n = 400
        offsets = [0.0 if (i // 15) % 2 == 0 else -1.0 for i in range(n)]
        state = build_state(offsets, [20.0] * n)
        result = lag.estimate(state, "radiators")
        assert not result.rise_measured
        assert not result.fall_measured

    def test_sub_noise_offset_movement_is_ignored(self):
        """Movements below EVENT_MIN_MOVE_C are drift, not deliberate actions."""
        n = 400
        tiny = lag.EVENT_MIN_MOVE_C / 10.0
        offsets = [0.0 if (i // 15) % 2 == 0 else -tiny for i in range(n)]
        indoors = [20.0 + (0.1 if (i // 15) % 2 else 0.0) for i in range(n)]
        result = lag.estimate(build_state(offsets, indoors), "radiators")
        assert not result.rise_measured

    def test_target_steps_alone_are_usable_excitation(self):
        """The comfort target is exogenous — a schedule firing is not caused by
        anything the house did — so it identifies the plant cleanly even when
        the offset itself never steps."""
        delay, half_period, n = 5, 15, 600
        targets = [21.0 if (i // half_period) % 2 == 0 else 23.0 for i in range(n)]
        indoors = [targets[max(0, i - delay)] for i in range(n)]
        offsets = [0.0] * n
        result = lag.estimate(build_state(offsets, indoors, targets), "radiators")
        assert result.rise_measured
        assert result.fall_measured
        assert result.rise_minutes == delay * STEP_MINUTES
        assert result.fall_minutes == delay * STEP_MINUTES

    def test_offset_events_during_a_setpoint_transient_are_discarded(self):
        """The confound that makes a naive correlation useless here.

        While the target is moving, the offset and the indoor temperature are
        both consequences of the setpoint change and routinely move in opposite
        directions — reading that as a response would invert the measurement.
        """
        offsets, indoors = make_series(4, 8)
        # Same data that measures cleanly with a stable target...
        assert lag.estimate(build_state(offsets, indoors), "radiators").rise_measured
        # ...becomes unreadable under a target that will not sit still. The
        # drift is deliberately gradual, so it produces no target events of its
        # own — it only invalidates the offset windows, which is precisely the
        # filter under test.
        drifting = [21.0 + 0.05 * i for i in range(len(offsets))]
        result = lag.estimate(build_state(offsets, indoors, drifting), "radiators")
        assert not result.rise_measured
        assert not result.fall_measured

    def test_a_window_is_valid_only_while_the_target_holds(self):
        steady = (21.0,) * 30
        assert lag._target_holds(steady, 0, 10)
        moved = (21.0,) * 5 + (23.0,) * 25
        assert not lag._target_holds(moved, 0, 10)
        # A change after the window ends is somebody else's problem.
        assert lag._target_holds(moved, 0, 3)

    def test_result_hours_helpers(self):
        state = build_state(*make_series(4, 8))
        result = lag.estimate(state, "radiators")
        assert result.rise_hours == result.rise_minutes / 60.0
        assert result.fall_hours == result.fall_minutes / 60.0

    def test_never_returns_a_non_positive_time(self):
        """Every consumer divides or schedules by these."""
        for state in (
            lag.initial_state(STEP_MINUTES),
            build_state(*make_series(1, 1)),
            build_state(*make_series(4, 8)),
        ):
            result = lag.estimate(state, None)
            assert result.rise_minutes > 0
            assert result.fall_minutes > 0
