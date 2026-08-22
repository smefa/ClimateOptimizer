"""How long this house takes to respond, measured rather than configured.

Pure module: standard library only, no dependency on any other module in this
package (not even `const.py`), so it is importable and unit-testable without
Home Assistant installed. CI never installs `homeassistant`, so this is what
makes the module testable at all.

## Why this exists

An integral controller with unmodelled dead time oscillates. It keeps winding
up while heat is already in the pipe, overshoots, unwinds, undershoots. So
`learner.py` cannot pick its integrator time constant without knowing how long
commanded heat actually takes to arrive, and the price logic in `heuristic.py`
cannot know how early to start coasting before an expensive hour.

Both numbers used to come from a dropdown: one `emitter_tau_floor_h` constant,
4 h for underfloor and 2 h for radiators, applied to every house of that type.
This module measures them instead.

## Two lags, not one

They are deliberately asymmetric, because the physics is:

    rise (L_up)    command -> measurable indoor response. The pump's own
                   controller integrating its curve error (a Nibe
                   degree-minutes accumulator is exactly this), plus the water
                   loop, plus emitter mass.

    fall (L_down)  stop calling for heat -> the house actually stops gaining.
                   A heavy slab keeps delivering long after the compressor
                   stops, so this is typically LONGER than the rise. It is the
                   one price braking is timed off, but only half of it: what
                   costs money is the compressor still drawing power, not the
                   slab coasting on stored heat afterward, and this measures
                   the latter along with the former.

## What is measured, and why it is not a cross-correlation

An earlier cut of this module correlated offset changes against indoor changes
at every candidate lag and took the argmax. That works beautifully on synthetic
step data and fails on a real closed loop, for a reason worth recording: in
feedback, the controller output and the plant output are correlated at ZERO lag
because the controller is reacting to the plant. That confound swamps the true
transport delay, and the correlation curve comes out monotonically decreasing
with a meaningless peak at lag zero.

So this measures something both better defined and more useful:

    the time from a deliberate change in the applied offset until the house has
    completed HALF of the move that change eventually produces

That is not pure transport delay — it folds in the emitter time constant — but
it is exactly the quantity both consumers actually want. "How long before this
action has meaningfully taken effect" is the right question for scheduling a
pre-brake, and the right thing to scale an integrator's time constant from.

Being event-triggered is what dodges the feedback confound: the measurement is
anchored to a discrete action and asks when a threshold was crossed afterwards,
rather than correlating two continuously coupled rates.

## Two sources of events, and one that must be thrown away

**Target steps** are the strongest signal, because the comfort target is
exogenous — nothing about the house causes it. A schedule, a manual nudge or a
voice command steps it, and the house answers after its own delay. This is the
textbook way to identify a plant, and it needs no disentangling.

**Offset steps** are usable too, but only while the target is holding still.
During a setpoint transient the offset and the indoor temperature are BOTH
consequences of the setpoint change, and they routinely move in opposite
directions: after the target drops, the house coasts down while the controller
simultaneously eases the offset back toward its steady value. Reading that as
"spoofing colder made the house colder" would invert the measurement outright.
So any offset event whose window contains a target change is discarded, and a
window in which the house moved the wrong way is discarded regardless.

It degrades honestly. With too few clean events, or events whose response is
below the sensor's own resolution, `estimate()` reports `measured=False` and
returns the emitter-type fallback rather than a confident wrong number. A house
whose target never moves and whose weather never shifts simply keeps the
fallback, which is the correct outcome: nothing happened, so nothing was
learned.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# --- Emitter-type fallbacks ------------------------------------------------
# Used until the estimator has a confident opinion, which is the
# only remaining reason the install asks what the emitters are. Rise/fall pairs
# are asymmetric for the reason in the module docstring.
HEATING_TYPE_RADIATORS = "radiators"
HEATING_TYPE_UNDERFLOOR = "underfloor"
HEATING_TYPE_MIXED = "mixed"
HEATING_TYPES = (HEATING_TYPE_RADIATORS, HEATING_TYPE_UNDERFLOOR, HEATING_TYPE_MIXED)
DEFAULT_HEATING_TYPE = HEATING_TYPE_RADIATORS

_FALLBACK_MINUTES: dict[str, tuple[float, float]] = {
    #                        rise,  fall
    HEATING_TYPE_RADIATORS: (90.0, 120.0),
    HEATING_TYPE_MIXED: (150.0, 240.0),
    HEATING_TYPE_UNDERFLOOR: (240.0, 360.0),
}

# --- Estimator tuning ------------------------------------------------------
# Longest response time the search considers, and therefore the width of the
# window examined after each event. Six hours covers a heavy slab.
MAX_LAG_MINUTES = 360.0
# How much history to keep. Four days at a 15-minute cadence — long enough to
# contain several independent events, short enough that a house whose behaviour
# changes (new emitters, a serviced pump) re-converges in days.
BUFFER_SAMPLES = 384
# How far the offset must move, in one direction, to count as a deliberate
# action rather than the integrator's ordinary drift.
EVENT_MIN_MOVE_C = 0.15
# ...and how quickly it must get there. A slow ramp spread over many hours is
# not step-like: the response would be smeared across the movement itself and
# the reading would measure the ramp rather than the house.
EVENT_MAX_STEPS = 8
# The indoor response must exceed this to be distinguishable from sensor
# quantisation, which is typically 0.1 degC.
MIN_RESPONSE_C = 0.08
# Clean events needed before a direction reports as measured. Low because each
# event is a direct observation rather than one noisy sample: three consistent
# answers beat a correlation over hundreds of coupled ones.
MIN_EVENTS = 3
# Fraction of the eventual response that counts as "has taken effect".
RESPONSE_FRACTION = 0.5
# An event's observation window is cut short by the next action in either
# direction — otherwise the reversal's response cancels this one's and the
# event reads as "the house did nothing". Below this many steps there is not
# enough room left to see anything, and the event is skipped.
MIN_WINDOW_STEPS = 2
# How far the comfort target must move to count as a deliberate setpoint step.
TARGET_STEP_C = 0.3


@dataclass(frozen=True)
class LagState:
    """Rolling history of what was asked for, what was applied, and what the
    house did.

    All three series are sampled on the caller's fixed cadence
    (`step_minutes`), one entry per learner step, oldest first. Storing plain
    aligned series rather than timestamps keeps every lookup a simple index
    shift.
    """

    offsets_c: tuple[float, ...] = ()
    indoors_c: tuple[float, ...] = ()
    targets_c: tuple[float, ...] = ()
    step_minutes: float = 15.0


@dataclass(frozen=True)
class LagResult:
    """Measured (or fallen-back) response times, in minutes."""

    rise_minutes: float
    fall_minutes: float
    rise_measured: bool
    fall_measured: bool
    rise_events: int
    fall_events: int
    samples: int
    reason: str

    @property
    def rise_hours(self) -> float:
        return self.rise_minutes / 60.0

    @property
    def fall_hours(self) -> float:
        return self.fall_minutes / 60.0


def fallback_minutes(heating_type: str | None) -> tuple[float, float]:
    """(rise, fall) fallback for an emitter type, defaulting to radiators."""
    return _FALLBACK_MINUTES.get(
        (heating_type or "").strip().lower(), _FALLBACK_MINUTES[DEFAULT_HEATING_TYPE]
    )


def initial_state(step_minutes: float = 15.0) -> LagState:
    return LagState(step_minutes=max(1.0, step_minutes))


def push(
    state: LagState, offset_c: float, indoor_c: float | None, target_c: float
) -> LagState:
    """Append one sample, dropping the oldest once the buffer is full.

    A missing indoor reading breaks the chain, so the whole buffer is dropped
    rather than stitched across the gap — a discontinuity would register as a
    huge fake event with a fabricated response. Rebuilding a few days of buffer
    is cheap; a corrupted estimate feeds straight into the integrator's time
    constant.
    """
    if indoor_c is None:
        return replace(state, offsets_c=(), indoors_c=(), targets_c=())
    return replace(
        state,
        offsets_c=(*state.offsets_c, float(offset_c))[-BUFFER_SAMPLES:],
        indoors_c=(*state.indoors_c, float(indoor_c))[-BUFFER_SAMPLES:],
        targets_c=(*state.targets_c, float(target_c))[-BUFFER_SAMPLES:],
    )


def _find_events(offsets: tuple[float, ...], rising: bool) -> list[int]:
    """Indices where a deliberate offset move in one direction began.

    Sign convention: the offset is added to the outdoor temperature the pump
    reads, so a MORE NEGATIVE offset spoofs colder and buys more heat. A rise
    event is therefore a downward run of the offset, and a fall event an upward
    one.

    A run qualifies only if it covers `EVENT_MIN_MOVE_C` within
    `EVENT_MAX_STEPS` of starting — decisive enough to be an action, and quick
    enough that the response can be told apart from the movement itself.
    """
    want = -1.0 if rising else 1.0
    events: list[int] = []
    index = 1
    while index < len(offsets):
        delta = offsets[index] - offsets[index - 1]
        if delta * want <= 0.0:
            index += 1
            continue
        # Walk the same-signed run from here, watching for the threshold.
        # The event is anchored at the first sample carrying the NEW offset,
        # not the one before it, so a measured response counts from when the
        # action was applied rather than one step early.
        start = index
        travelled = 0.0
        steps = 0
        cursor = index
        qualified = False
        while cursor < len(offsets):
            move = offsets[cursor] - offsets[cursor - 1]
            if move * want <= 0.0:
                break
            travelled += abs(move)
            steps += 1
            if travelled >= EVENT_MIN_MOVE_C and steps <= EVENT_MAX_STEPS:
                qualified = True
                cursor += 1
                break
            if steps > EVENT_MAX_STEPS:
                break
            cursor += 1
        if qualified:
            events.append(start)
            # Skip the rest of this run so one long move is one event.
            while cursor < len(offsets):
                move = offsets[cursor] - offsets[cursor - 1]
                if move * want <= 0.0:
                    break
                cursor += 1
        index = max(cursor, index + 1)
    return events


def _response_steps(
    indoors: tuple[float, ...], start: int, window: int, rising: bool
) -> int | None:
    """Steps from an event until half its eventual response had arrived.

    Returns None when the window runs past the end of the data, when the house
    moved the wrong way (so this event's response is confounded by something
    else), or when the total move is below the sensor's own resolution.
    """
    end = start + window
    if end >= len(indoors):
        return None
    baseline = indoors[start]
    total = indoors[end] - baseline
    if rising and total < MIN_RESPONSE_C:
        return None
    if not rising and total > -MIN_RESPONSE_C:
        return None
    threshold = abs(total) * RESPONSE_FRACTION
    for offset_steps in range(1, window + 1):
        if abs(indoors[start + offset_steps] - baseline) >= threshold:
            return offset_steps
    return None


def _find_target_events(targets: tuple[float, ...], rising: bool) -> list[int]:
    """Indices where the comfort target stepped.

    The cleanest excitation available, because the target is exogenous: nothing
    about the house causes a schedule to fire. A target increase should be
    followed by indoor rising, so it is a rise event.
    """
    want = 1.0 if rising else -1.0
    return [
        index
        for index in range(1, len(targets))
        if (targets[index] - targets[index - 1]) * want >= TARGET_STEP_C
    ]


def _target_holds(targets: tuple[float, ...], start: int, window: int) -> bool:
    """Whether the target stayed put across an event's whole window.

    Offset events are only readable while it does. During a setpoint transient
    the offset and the indoor temperature are both consequences of the setpoint
    change and routinely move in opposite directions, which would invert the
    measurement.
    """
    if start >= len(targets):
        return False
    baseline = targets[start]
    end = min(start + window + 1, len(targets))
    return all(abs(targets[i] - baseline) < TARGET_STEP_C for i in range(start, end))


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _candidate_events(state: LagState, rising: bool) -> list[tuple[int, bool]]:
    """(index, is_target_step) for every event worth trying, in order."""
    events = [(i, True) for i in _find_target_events(state.targets_c, rising)]
    events += [(i, False) for i in _find_events(state.offsets_c, rising)]
    return sorted(set(events))


def _measure(
    state: LagState, window: int, rising: bool, boundaries: list[int]
) -> tuple[float | None, int]:
    """(median response in steps, clean events), or (None, events) if too few.

    Each event's window is truncated at the next action in either direction, so
    one event's response is never measured through the reversal that cancels
    it. `boundaries` is every event index, both directions, in order.

    The median rather than the mean: one event contaminated by an open window
    or a burst of sun should not drag the estimate, and with a handful of
    samples a single outlier easily would.
    """
    responses: list[float] = []
    for start, is_target_step in _candidate_events(state, rising):
        limit = window
        for boundary in boundaries:
            if boundary > start:
                # Strictly before the next action: the sample at which it was
                # applied already belongs to that event, not this one.
                limit = min(window, boundary - start - 1)
                break
        if limit < MIN_WINDOW_STEPS:
            continue
        # An offset event during a setpoint transient is unreadable; a target
        # event is only readable if the target then holds long enough to see
        # the house arrive.
        if not _target_holds(state.targets_c, start, limit):
            continue
        response = _response_steps(state.indoors_c, start, limit, rising)
        if response is not None:
            responses.append(float(response))
    if len(responses) < MIN_EVENTS:
        return None, len(responses)
    return _median(responses), len(responses)


def estimate(state: LagState, heating_type: str | None) -> LagResult:
    """Measure rise and fall times, falling back to the emitter-type defaults.

    Never raises and never returns a non-positive time: every downstream
    consumer divides or schedules by these, so an unusable estimate has to
    surface as the fallback rather than as a zero.
    """
    fb_rise, fb_fall = fallback_minutes(heating_type)
    step = max(1.0, state.step_minutes)
    samples = len(state.indoors_c)
    window = max(1, int(MAX_LAG_MINUTES / step))

    if samples < window + 2:
        return LagResult(
            rise_minutes=fb_rise,
            fall_minutes=fb_fall,
            rise_measured=False,
            fall_measured=False,
            rise_events=0,
            fall_events=0,
            samples=samples,
            reason=(
                f"Building history ({samples} samples); using "
                f"{heating_type or DEFAULT_HEATING_TYPE} defaults of "
                f"{fb_rise:.0f} min rise / {fb_fall:.0f} min fall"
            ),
        )

    boundaries = sorted(
        index
        for rising in (True, False)
        for index, _ in _candidate_events(state, rising)
    )
    rise_steps, rise_events = _measure(state, window, True, boundaries)
    fall_steps, fall_events = _measure(state, window, False, boundaries)

    rise_minutes = fb_rise if rise_steps is None else max(step, rise_steps * step)
    fall_minutes = fb_fall if fall_steps is None else max(step, fall_steps * step)

    parts = [
        f"rise {rise_minutes:.0f} min "
        + (
            f"measured from {rise_events} events"
            if rise_steps is not None
            else f"(fallback, {rise_events} usable events)"
        ),
        f"fall {fall_minutes:.0f} min "
        + (
            f"measured from {fall_events} events"
            if fall_steps is not None
            else f"(fallback, {fall_events} usable events)"
        ),
    ]

    return LagResult(
        rise_minutes=rise_minutes,
        fall_minutes=fall_minutes,
        rise_measured=rise_steps is not None,
        fall_measured=fall_steps is not None,
        rise_events=rise_events,
        fall_events=fall_events,
        samples=samples,
        reason="; ".join(parts),
    )
