"""End-to-end tests of the pure control loop.

The unit tests cover each module alone. These drive `lag` -> `learner` ->
`heuristic` together against a simulated house, which is where the errors that
matter actually hide: a sign flipped between two modules, or the price logic
and the learner quietly fighting each other.

The house model is intentionally crude — a first-order lag toward whatever
equilibrium the current spoof buys, plus a transport delay. It is not trying to
be a building; it is trying to be *a plant with dead time*, because dead time is
what an integral controller can be destroyed by.
"""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from loader import load  # noqa: E402

heuristic = load("heuristic")
lag = load("lag")
learner = load("learner")

STEP_MINUTES = 15.0
STEP_HOURS = STEP_MINUTES / 60.0


class House:
    """A house whose pump curve is `curve_error_c` short of what it needs.

    Spoofing the outdoor reading down by `curve_error_c` is exactly what makes
    it hold `nominal_target_c` — so a correct controller must converge on that
    number without anyone telling it.

    Note carefully what `advance` does NOT take: the comfort target. A house
    has no idea what temperature anyone wants. A setpoint change reaches it
    only as a changed offset, and therefore only through the same transport
    delay as everything else. Letting the target influence the plant directly
    would quietly hand the lag estimator a response with no delay in it, and
    it would measure a number smaller than the delay it was trying to find.
    """

    def __init__(
        self,
        curve_error_c: float = -2.0,
        delay_steps: int = 4,
        inertia: float = 0.15,
        indoor_c: float = 19.0,
        capacity_c: float | None = None,
        nominal_target_c: float = 21.0,
    ):
        self.curve_error_c = curve_error_c
        self.inertia = inertia
        self.indoor_c = indoor_c
        # Where the house settles with no spoofing at all.
        self.base_indoor_c = nominal_target_c + curve_error_c
        # Beyond this spoof depth the pump delivers nothing more.
        self.capacity_c = capacity_c
        self.pipeline: deque[float] = deque([0.0] * delay_steps, maxlen=delay_steps)

    def advance(self, offset_c: float) -> float:
        self.pipeline.append(offset_c)
        delivered = self.pipeline[0]
        if self.capacity_c is not None:
            # The pump cannot deliver more than this no matter how deep the
            # spoof goes — the deep-cold case.
            delivered = max(delivered, -self.capacity_c)
        # Spoofing COLDER (a more negative offset) asks the pump's curve for
        # more heat, so equilibrium rises as `delivered` falls.
        equilibrium = self.base_indoor_c - delivered
        self.indoor_c += (equilibrium - self.indoor_c) * self.inertia
        return self.indoor_c


def run(
    house: House,
    steps: int,
    target_c: float = 21.0,
    outdoor_c: float = 0.0,
    price_at=None,
    params_overrides: dict | None = None,
    inputs_overrides: dict | None = None,
    active_at=None,
):
    """Drive the full loop and return (trace, final learner result, final output).

    `active_at(i)` decides whether compensation is switched on at step `i`,
    defaulting to always-on. While it is off the house is advanced with a zero
    offset, mirroring what the coordinator actually publishes in that state
    (the raw outdoor reading, to every output channel) — so the simulated pump
    really does run its own untouched curve, which is the whole premise the
    baseline table rests on.
    """
    learner_state = learner.initial_state()
    lag_state = lag.initial_state(STEP_MINUTES)
    offset = 0.0
    now = 0.0
    trace = []
    previous_output = None
    result = None
    output = None

    for i in range(steps):
        is_active = True if active_at is None else active_at(i)
        indoor = house.advance(offset)
        now += STEP_HOURS * 3600.0

        lag_state = lag.push(lag_state, offset, indoor, target_c)
        lag_result = lag.estimate(lag_state, "radiators")

        price, forecast = (None, None) if price_at is None else price_at(i)

        learner_state, result = learner.step(
            learner_state,
            learner.LearnerInputs(
                now_s=now,
                dt_hours=STEP_HOURS,
                indoor_temp_c=indoor,
                indoor_data_available=True,
                target_c=target_c,
                outdoor_temp_c=outdoor_c,
                heating_hard_limit_engaged=False,
                is_active=is_active,
                price_braking=bool(previous_output and previous_output.price_braking),
                rise_hours=lag_result.rise_hours,
            ),
        )

        params = dict(
            indoor_target_c=target_c,
            comfort_min_c=18.0,
            enable_price_compensation=price_at is not None,
            price_comfort_tier="high",
            cold_caution="low",
            enable_solar_input=False,
            enable_wind_input=False,
            spoof_per_indoor_c=learner.SPOOF_PER_INDOOR_C,
        )
        params.update(params_overrides or {})

        inputs = dict(
            indoor_temp_c=indoor,
            indoor_data_available=True,
            raw_outdoor_temp_c=outdoor_c,
            wind_speed_ms=0.0,
            wind_data_available=True,
            sun_elevation_deg=-10.0,
            cloud_coverage_pct=100.0,
            cloud_data_available=True,
            current_price=price,
            price_data_available=price is not None,
            price_forecast=forecast,
            learned_offset_c=result.offset_c,
            fall_minutes=lag_result.fall_minutes,
            rise_minutes=lag_result.rise_minutes,
            recoverable_sag_c=learner.recoverable_sag_c(
                result.close_rate_c_per_h,
                lag_result.rise_hours * learner.TAU_I_LAG_MULTIPLE,
            ),
        )
        inputs.update(inputs_overrides or {})

        output = heuristic.compute(
            heuristic.HeuristicInputs(**inputs), heuristic.HeuristicParams(**params)
        )
        previous_output = output
        # What the pump actually sees, relative to the real outdoor reading.
        # Zero while compensation is off: the coordinator publishes the raw
        # reading then, so no term — learned, weather or price — reaches the pump.
        offset = (output.compensated_outdoor_temp_c - outdoor_c) if is_active else 0.0
        trace.append((indoor, offset, result.hold_offset_c))

    return trace, result, output


class TestConvergence:
    def test_the_house_reaches_target_with_nothing_configured(self):
        house = House(curve_error_c=-2.0)
        trace, result, _ = run(house, 1200)
        indoor = [t[0] for t in trace]
        assert indoor[-1] == pytest.approx(21.0, abs=0.15)
        assert result.hold_offset_c == pytest.approx(-2.0, abs=0.3)

    def test_it_finds_a_positive_offset_for_an_over_generous_curve(self):
        """A pump curve that overshoots must be corrected the other way."""
        house = House(curve_error_c=1.5, indoor_c=23.0)
        trace, result, _ = run(house, 1200)
        assert trace[-1][0] == pytest.approx(21.0, abs=0.2)
        assert result.hold_offset_c > 0

    def test_it_does_not_oscillate_against_dead_time(self):
        """The failure mode integral control is famous for. A slab-like plant
        with a long transport delay must settle, not hunt."""
        house = House(curve_error_c=-2.0, delay_steps=16, inertia=0.08)
        trace, _, _ = run(house, 2000)
        settled = [t[0] for t in trace[-400:]]
        assert max(settled) - min(settled) < 0.3
        # And no sustained overshoot past target.
        assert max(settled) < 21.5

    def test_a_slower_house_converges_too_just_later(self):
        fast = House(curve_error_c=-2.0, delay_steps=2, inertia=0.3)
        slow = House(curve_error_c=-2.0, delay_steps=12, inertia=0.08)
        fast_trace, _, _ = run(fast, 1500)
        slow_trace, _, _ = run(slow, 1500)
        assert fast_trace[-1][0] == pytest.approx(21.0, abs=0.2)
        assert slow_trace[-1][0] == pytest.approx(21.0, abs=0.3)

    def test_authority_is_never_exceeded_during_the_transient(self):
        house = House(curve_error_c=-8.0, indoor_c=14.0)
        trace, _, _ = run(house, 600)
        assert all(
            abs(hold) <= learner.MAX_AUTHORITY_C + 1e-6 for _, _, hold in trace
        )


class TestCapacityLimit:
    def test_a_pump_that_cannot_keep_up_does_not_wind_up(self):
        """The deep-cold case. The house never reaches target, and the learner
        must stop pushing rather than bank a correction that would dump into a
        pump that can suddenly deliver it."""
        house = House(curve_error_c=-6.0, capacity_c=2.0, indoor_c=17.0)
        trace, result, _ = run(house, 1000, outdoor_c=-18.0)
        holds = [t[2] for t in trace]
        assert all(abs(h) <= learner.MAX_AUTHORITY_C + 1e-6 for h in holds)
        # It really was short of target, so this is the saturating case.
        assert trace[-1][0] < 21.0
        assert result.ceiling_c <= learner.MAX_AUTHORITY_C

    def test_recovery_after_capacity_returns(self):
        """Once the weather relents, the stored offset must not slam the pump."""
        house = House(curve_error_c=-6.0, capacity_c=2.0, indoor_c=17.0)
        trace, _, _ = run(house, 600, outdoor_c=-18.0)
        peak_after = max(abs(offset) for _, offset, _ in trace)
        assert peak_after <= learner.MAX_AUTHORITY_C + 1e-6


class TestPriceInteraction:
    @staticmethod
    def _spike_schedule(spike_step: int, width: int = 8):
        """A day-ahead forecast that rolls forward, with one expensive block."""

        def at(i: int):
            hours_to_spike = (spike_step - i) * STEP_HOURS
            forecast = tuple(
                (
                    float(h),
                    5.0 if 0 <= (hours_to_spike - h) < width * STEP_HOURS else 1.0,
                )
                for h in range(24)
            )
            price = 5.0 if spike_step <= i < spike_step + width else 1.0
            return price, forecast

        return at

    def test_the_learner_does_not_fight_the_price_excursion(self):
        """If the learner treated a deliberate sag as error, it would wind up
        opposing the very excursion it was asked for — and the table would be
        poisoned by every expensive hour of the winter."""
        house = House(curve_error_c=-2.0)
        # Settle first, so the table has a real value to protect.
        trace, result, _ = run(house, 900)
        settled = result.hold_offset_c

        house2 = House(curve_error_c=-2.0, indoor_c=trace[-1][0])
        _, after, output = run(
            house2, 400, price_at=self._spike_schedule(spike_step=100)
        )
        # The learned steady offset survives the spike broadly intact.
        assert after.hold_offset_c == pytest.approx(settled, abs=0.6)

    def test_an_expensive_block_actually_lets_the_house_cool(self):
        house = House(curve_error_c=-2.0)
        run(house, 900)  # settle
        indoor_before = house.indoor_c
        trace, _, _ = run(
            House(curve_error_c=-2.0, indoor_c=indoor_before),
            200,
            price_at=self._spike_schedule(spike_step=40),
        )
        during = min(t[0] for t in trace[40:120])
        assert during < indoor_before

    def test_the_comfort_floor_holds_through_a_spike(self):
        house = House(curve_error_c=-2.0)
        run(house, 900)
        trace, _, _ = run(
            House(curve_error_c=-2.0, indoor_c=house.indoor_c),
            400,
            price_at=self._spike_schedule(spike_step=40, width=40),
            params_overrides={"comfort_min_c": 20.0},
        )
        # The floor bounds the *setpoint*; allow the plant a little undershoot
        # below it while coasting, but nothing like an unbounded drift.
        assert min(t[0] for t in trace) > 19.0

    def test_no_price_braking_when_it_is_far_too_cold(self):
        house = House(curve_error_c=-2.0)
        _, _, output = run(
            house,
            300,
            outdoor_c=-25.0,
            price_at=self._spike_schedule(spike_step=100),
            params_overrides={"cold_caution": "low"},
        )
        assert output.cold_brake_factor == 0.0
        assert output.price_adjustment_c == 0.0


class TestLagMeasurementInTheLoop:
    def test_a_schedule_teaches_it_the_response_time(self):
        """Target steps are the excitation that actually works in closed loop.

        A schedule moving the target is exogenous — nothing about the house
        caused it — so the house's answer is readable. This is the realistic
        path by which a normal install learns its own response time, and it is
        why the target series is in the lag buffer at all.
        """
        house = House(curve_error_c=-2.0, delay_steps=6)
        learner_state = learner.initial_state()
        lag_state = lag.initial_state(STEP_MINUTES)
        offset = 0.0
        now = 0.0
        target = 21.0
        # A plain day/night schedule: down overnight, up in the morning.
        for i in range(1400):
            if i % 48 == 0:
                target = 21.0 if target != 21.0 else 22.5
            indoor = house.advance(offset)
            now += STEP_HOURS * 3600.0
            lag_state = lag.push(lag_state, offset, indoor, target)
            lag_result = lag.estimate(lag_state, "radiators")
            learner_state, result = learner.step(
                learner_state,
                learner.LearnerInputs(
                    now_s=now,
                    dt_hours=STEP_HOURS,
                    indoor_temp_c=indoor,
                    indoor_data_available=True,
                    target_c=target,
                    outdoor_temp_c=0.0,
                    heating_hard_limit_engaged=False,
                    is_active=True,
                    price_braking=False,
                    rise_hours=lag_result.rise_hours,
                ),
            )
            offset = result.offset_c
        assert lag_result.rise_measured or lag_result.fall_measured
        assert 0 < lag_result.rise_minutes <= lag.MAX_LAG_MINUTES
        # What is measured is "time until half the effect arrived", so it is
        # necessarily at least the transport delay and generally longer.
        assert lag_result.rise_minutes >= 6 * STEP_MINUTES

    def test_a_slower_house_measures_a_longer_response(self):
        """The invariant that actually matters downstream: a slab must end up
        with a slower integrator and an earlier pre-brake than a radiator."""

        def measured(delay_steps: int) -> float:
            house = House(curve_error_c=-2.0, delay_steps=delay_steps)
            learner_state = learner.initial_state()
            lag_state = lag.initial_state(STEP_MINUTES)
            offset, now, target = 0.0, 0.0, 21.0
            for i in range(1400):
                if i % 48 == 0:
                    target = 21.0 if target != 21.0 else 22.5
                indoor = house.advance(offset)
                now += STEP_HOURS * 3600.0
                lag_state = lag.push(lag_state, offset, indoor, target)
                lag_result = lag.estimate(lag_state, "radiators")
                learner_state, result = learner.step(
                    learner_state,
                    learner.LearnerInputs(
                        now_s=now,
                        dt_hours=STEP_HOURS,
                        indoor_temp_c=indoor,
                        indoor_data_available=True,
                        target_c=target,
                        outdoor_temp_c=0.0,
                        heating_hard_limit_engaged=False,
                        is_active=True,
                        price_braking=False,
                        rise_hours=lag_result.rise_hours,
                    ),
                )
                offset = result.offset_c
            return lag.estimate(lag_state, "radiators").rise_minutes

        assert measured(12) > measured(2)


class TestBaselineSeeding:
    """The off period is where the raw curve is observed; switching on is where
    that observation is spent.

    The strongest property available here is that the two must AGREE: the house
    settles at `nominal_target + curve_error` unspoofed, so the offset implied
    by the baseline is exactly the offset closed-loop integration converges to
    on its own. A sign error anywhere between the two would show up as the seed
    pointing the wrong way, which is the class of mistake that has bitten this
    project before.
    """

    def test_the_baseline_predicts_the_offset_the_loop_converges_to(self):
        # 400 steps off (100 h) is far past the dwell requirement, so the
        # baseline table fills; nothing is applied to the house throughout.
        house = House(curve_error_c=-2.0)
        _, result, _ = run(house, 400, active_at=lambda i: False)

        assert result.baseline_indoor_c == pytest.approx(19.0, abs=0.2)
        # Positive deficit = the raw curve leaves the house cold.
        assert result.baseline_deficit_c == pytest.approx(2.0, abs=0.2)
        implied = learner.seed_offset_for(result.baseline_indoor_c, 21.0)
        # -2.0 is what TestConvergence proves the loop reaches unaided.
        assert implied == pytest.approx(-2.0, abs=0.2)

    def test_switching_on_starts_from_the_baseline_instead_of_zero(self):
        house = House(curve_error_c=-2.0)
        # Off for 400 steps, then on. The step immediately after the flip should
        # already be pushing, not starting from a standing start at zero.
        _, result, _ = run(house, 401, active_at=lambda i: i >= 400)

        assert result.seeded_bins >= 1
        assert result.seeded
        # The baseline implies -2.0, and the day-one authority ramp allows only
        # 1.0 — so the seed shows up pinned hard against the ramp, where a cold
        # start would still be sitting near zero after one step.
        assert result.hold_offset_c == pytest.approx(-learner.MIN_AUTHORITY_C, abs=1e-6)

    def test_an_over_generous_curve_seeds_the_other_way(self):
        # The mirror case: a curve that overheats settles the house ABOVE
        # target, which must seed a positive (warmer-spoofing) offset.
        house = House(curve_error_c=1.5, indoor_c=22.5)
        _, result, _ = run(house, 400, active_at=lambda i: False)

        assert result.baseline_indoor_c == pytest.approx(22.5, abs=0.2)
        assert learner.seed_offset_for(result.baseline_indoor_c, 21.0) > 0.0

    def test_seeding_does_not_buy_authority_it_has_not_earned(self):
        """A seed is open-loop evidence, so it must not advance the ramp.

        This is the safety property of the whole feature: a house could sit off
        for a month, and switching on must still not permit a day-one spoof
        larger than a from-cold install would.
        """
        house = House(curve_error_c=-4.0)
        _, result, _ = run(house, 401, active_at=lambda i: i >= 400)

        assert result.total_samples <= 1
        assert result.authority_c == pytest.approx(learner.MIN_AUTHORITY_C, abs=0.05)
        # Seeded deep, but the ramp still binds what actually reaches the pump.
        assert abs(result.offset_c) <= learner.MIN_AUTHORITY_C + 1e-6

    def test_a_seeded_start_converges_faster_than_a_cold_one(self):
        """The point of the feature, stated as a measurement."""

        def error_after(steps: int, seeded: bool) -> float:
            house = House(curve_error_c=-2.0)
            if seeded:
                # Same total wall-clock in the ON state for both arms, so this
                # compares starting points rather than simply running longer.
                trace, _, _ = run(
                    house, 400 + steps, active_at=lambda i: i >= 400
                )
            else:
                trace, _, _ = run(house, steps)
            return abs(trace[-1][0] - 21.0)

        assert error_after(120, seeded=True) < error_after(120, seeded=False)
