"""Unit tests for the term-composition and price logic.

What used to be the whole controller is now composition plus price. The indoor
response lives in `learner.py` and arrives here as a finished number, so these
tests are about how terms combine, when price acts, and — the part with real
teeth — when price refuses to act because it is too cold to buy the sag back.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from loader import load  # noqa: E402

heuristic = load("heuristic")

HeuristicInputs = heuristic.HeuristicInputs
HeuristicParams = heuristic.HeuristicParams
compute = heuristic.compute


def make_params(**overrides) -> HeuristicParams:
    defaults = dict(
        indoor_target_c=21.0,
        comfort_min_c=18.0,
        # High enough that the default raw_outdoor_temp_c never trips it;
        # cutoff tests override it explicitly.
        heating_cutoff_c=100.0,
        enable_price_compensation=False,
    )
    defaults.update(overrides)
    return HeuristicParams(**defaults)


def make_inputs(**overrides) -> HeuristicInputs:
    defaults = dict(
        indoor_temp_c=21.0,
        indoor_data_available=True,
        raw_outdoor_temp_c=3.0,
        wind_speed_ms=0.0,
        wind_data_available=True,
        sun_elevation_deg=0.0,
        cloud_coverage_pct=0.0,
        cloud_data_available=True,
        current_price=None,
        price_data_available=False,
    )
    defaults.update(overrides)
    return HeuristicInputs(**defaults)


def flat_forecast(price: float = 1.0, hours: int = 24):
    return tuple((float(h), price) for h in range(hours))


def spiky_forecast(base: float = 1.0, peak: float = 5.0, spike_at: int = 4, hours: int = 24):
    """A day with one clear spike, which is what the band logic is tuned for."""
    return tuple(
        (float(h), peak if h == spike_at else base) for h in range(hours)
    )


class TestSolarEffectOf:
    """Direct tests of the extracted formula, since `compute()` now just calls
    it — see `TestComposition.test_compute_still_uses_the_extracted_formula`
    for confirmation the extraction did not change `compute()`'s behaviour."""

    def test_sun_below_horizon_is_zero(self):
        assert heuristic.solar_effect_of(-20.0, 0.0) == 0.0

    def test_full_cloud_cover_is_zero(self):
        assert heuristic.solar_effect_of(45.0, 100.0) == 0.0

    def test_missing_cloud_data_is_treated_as_clear(self):
        """Same "assume clear" behaviour `compute()` documents for a missing
        forecast — None must not collapse the term to zero."""
        assert heuristic.solar_effect_of(90.0, None) == pytest.approx(1.0)


class TestComposition:
    def test_compute_still_uses_the_extracted_formula(self):
        """Confirms the extraction in `solar_effect_of` was a pure refactor:
        `compute()`'s own solar_effect must exactly match calling the
        extracted function directly with the same inputs."""
        result = compute(
            make_inputs(sun_elevation_deg=45.0, cloud_coverage_pct=30.0),
            make_params(),
        )
        assert result.solar_effect == pytest.approx(
            heuristic.solar_effect_of(45.0, 30.0)
        )


    def test_learned_offset_passes_straight_through(self):
        result = compute(make_inputs(learned_offset_c=-2.5), make_params())
        assert result.compensated_outdoor_temp_c == pytest.approx(0.5)
        assert result.learned_offset_c == pytest.approx(-2.5)

    def test_terms_sum_onto_the_raw_reading(self):
        result = compute(
            make_inputs(
                raw_outdoor_temp_c=0.0,
                learned_offset_c=-1.0,
                wind_speed_ms=10.0,
                sun_elevation_deg=90.0,
                cloud_coverage_pct=0.0,
            ),
            make_params(),
        )
        expected = (
            0.0
            + result.learned_offset_c
            + result.wind_adjustment_c
            + result.sun_adjustment_c
            + result.price_adjustment_c
        )
        assert result.compensated_outdoor_temp_c == pytest.approx(expected)

    def test_wind_asks_for_more_heat_and_sun_asks_for_less(self):
        result = compute(
            make_inputs(
                wind_speed_ms=8.0, sun_elevation_deg=45.0, cloud_coverage_pct=0.0
            ),
            make_params(),
        )
        assert result.wind_adjustment_c < 0
        assert result.sun_adjustment_c > 0

    def test_cloud_cover_scales_the_solar_term(self):
        clear = compute(
            make_inputs(sun_elevation_deg=45.0, cloud_coverage_pct=0.0), make_params()
        )
        overcast = compute(
            make_inputs(sun_elevation_deg=45.0, cloud_coverage_pct=100.0), make_params()
        )
        assert clear.sun_adjustment_c > overcast.sun_adjustment_c
        assert overcast.sun_adjustment_c == pytest.approx(0.0)

    def test_night_contributes_no_solar(self):
        result = compute(make_inputs(sun_elevation_deg=-20.0), make_params())
        assert result.solar_effect == 0.0
        assert result.sun_adjustment_c == 0.0

    def test_disabled_inputs_contribute_exactly_zero(self):
        result = compute(
            make_inputs(
                wind_speed_ms=10.0, sun_elevation_deg=45.0, cloud_coverage_pct=0.0
            ),
            make_params(enable_wind_input=False, enable_solar_input=False),
        )
        assert result.wind_adjustment_c == 0.0
        assert result.sun_adjustment_c == 0.0
        assert "wind off" in result.reason
        assert "solar off" in result.reason

    def test_solar_effect_is_reported_even_when_the_term_is_off(self):
        """It is a physical fact about the world, and the log wants reality."""
        result = compute(
            make_inputs(sun_elevation_deg=90.0, cloud_coverage_pct=0.0),
            make_params(enable_solar_input=False),
        )
        assert result.solar_effect == pytest.approx(1.0)
        assert result.sun_adjustment_c == 0.0

    def test_output_is_clamped_to_sane_bounds(self):
        low = compute(
            make_inputs(raw_outdoor_temp_c=-39.0, learned_offset_c=-50.0), make_params()
        )
        assert low.compensated_outdoor_temp_c == heuristic.OUTPUT_SANITY_MIN_C
        high = compute(
            make_inputs(raw_outdoor_temp_c=20.0, learned_offset_c=50.0),
            make_params(heating_cutoff_c=100.0),
        )
        assert high.compensated_outdoor_temp_c == heuristic.OUTPUT_SANITY_MAX_C

    def test_missing_indoor_reading_still_publishes(self):
        result = compute(
            make_inputs(
                indoor_temp_c=None, indoor_data_available=False, learned_offset_c=-1.0
            ),
            make_params(),
        )
        assert result.compensated_outdoor_temp_c == pytest.approx(2.0)
        assert "Indoor sensor unavailable" in result.reason


class TestHeatingCutoff:
    def test_above_cutoff_everything_is_suppressed(self):
        result = compute(
            make_inputs(
                raw_outdoor_temp_c=18.0,
                learned_offset_c=-3.0,
                wind_speed_ms=10.0,
                sun_elevation_deg=45.0,
                current_price=9.0,
                price_data_available=True,
                price_forecast=spiky_forecast(),
            ),
            make_params(heating_cutoff_c=18.0, enable_price_compensation=True),
        )
        assert result.heating_cutoff_engaged
        assert result.compensated_outdoor_temp_c == 18.0
        assert result.learned_offset_c == 0.0
        assert result.wind_adjustment_c == 0.0
        assert result.sun_adjustment_c == 0.0
        assert result.price_adjustment_c == 0.0
        assert result.current_price == 9.0

    def test_just_below_cutoff_still_compensates(self):
        result = compute(
            make_inputs(raw_outdoor_temp_c=17.9, learned_offset_c=-1.0),
            make_params(heating_cutoff_c=18.0),
        )
        assert not result.heating_cutoff_engaged
        assert result.compensated_outdoor_temp_c == pytest.approx(16.9)


class TestHeatingCutoffHysteresis:
    """Reproduces the reported symptom: `sensor.*_heat_pump_offset` flipping
    between its full value and 0 because raw outdoor temperature idles right
    on `heating_cutoff_c`. Without hysteresis, `test_just_below_cutoff_still_
    compensates` above and `test_above_cutoff_everything_is_suppressed` would
    both fire, alternately, on every cycle the reading crosses back and
    forth — that flip is exactly what these tests close off."""

    def test_resolve_engaged_is_a_bare_threshold_when_not_previously_engaged(self):
        engaged = heuristic.resolve_heating_cutoff_engaged
        assert not engaged(17.9, 18.0, prev_engaged=False)
        assert engaged(18.0, 18.0, prev_engaged=False)

    def test_resolve_engaged_requires_the_margin_to_release(self):
        engaged = heuristic.resolve_heating_cutoff_engaged
        margin = heuristic.HEATING_CUTOFF_HYSTERESIS_C
        # Still within the margin below cutoff: stays engaged.
        assert engaged(18.0 - margin + 0.1, 18.0, prev_engaged=True)
        # Past the margin: releases.
        assert not engaged(18.0 - margin - 0.1, 18.0, prev_engaged=True)

    def test_previously_engaged_stays_suppressed_just_below_cutoff(self):
        """Same 17.9°C reading as `test_just_below_cutoff_still_compensates`,
        but arriving with last cycle's cutoff already engaged — the case
        that formula-only comparison got wrong."""
        result = compute(
            replace(
                make_inputs(raw_outdoor_temp_c=17.9, learned_offset_c=-1.0),
                prev_heating_cutoff_engaged=True,
            ),
            make_params(heating_cutoff_c=18.0),
        )
        assert result.heating_cutoff_engaged
        assert result.compensated_outdoor_temp_c == 17.9
        assert result.learned_offset_c == 0.0

    def test_releases_once_past_the_hysteresis_margin(self):
        margin = heuristic.HEATING_CUTOFF_HYSTERESIS_C
        result = compute(
            replace(
                make_inputs(
                    raw_outdoor_temp_c=18.0 - margin - 0.1, learned_offset_c=-1.0
                ),
                prev_heating_cutoff_engaged=True,
            ),
            make_params(heating_cutoff_c=18.0),
        )
        assert not result.heating_cutoff_engaged
        assert result.compensated_outdoor_temp_c == pytest.approx(
            18.0 - margin - 0.1 - 1.0
        )


class TestPriceGating:
    def test_no_price_entity_means_no_price_action(self):
        result = compute(make_inputs(), make_params(enable_price_compensation=True))
        assert result.price_adjustment_c == 0.0
        assert not result.price_braking

    def test_disabled_price_ignores_a_real_spike(self):
        result = compute(
            make_inputs(
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=0),
            ),
            make_params(enable_price_compensation=False),
        )
        assert result.price_adjustment_c == 0.0

    def test_no_forecast_means_no_price_action(self):
        """Deliberate: without a day-distribution there is no principled way to
        know whether the current price is high. Two configured absolute
        thresholds were both a config burden and a way to brake on an ordinary
        day."""
        result = compute(
            make_inputs(current_price=99.0, price_data_available=True, price_forecast=None),
            make_params(enable_price_compensation=True),
        )
        assert result.price_adjustment_c == 0.0
        assert "no day-ahead forecast" in result.reason

    def test_too_few_forecast_points_means_no_price_action(self):
        result = compute(
            make_inputs(
                current_price=99.0,
                price_data_available=True,
                price_forecast=((0.0, 1.0), (1.0, 9.0)),
            ),
            make_params(enable_price_compensation=True),
        )
        assert result.price_adjustment_c == 0.0

    def test_flat_day_is_left_alone(self):
        result = compute(
            make_inputs(
                current_price=1.0,
                price_data_available=True,
                price_forecast=flat_forecast(),
            ),
            make_params(enable_price_compensation=True),
        )
        assert result.price_adjustment_c == 0.0
        assert "flat day" in result.reason

    def test_disabled_compensation_still_reports_the_raw_feed(self):
        """current_price and price_median are informational — a house with
        braking switched off can still ask what power costs right now. Only
        the brake thresholds are supposed to disappear."""
        result = compute(
            make_inputs(
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=0),
            ),
            make_params(enable_price_compensation=False),
        )
        assert result.current_price == 5.0
        assert result.price_median is not None
        assert result.price_adjustment_c == 0.0
        assert result.price_band_start is None
        assert result.price_band_full is None


class TestPriceBraking:
    def _braking(self, **overrides):
        params = dict(
            enable_price_compensation=True,
            price_comfort_tier="high",
            cold_caution="low",
        )
        params.update(overrides.pop("params", {}))
        inputs = dict(
            raw_outdoor_temp_c=3.0,
            current_price=5.0,
            price_data_available=True,
            price_forecast=spiky_forecast(spike_at=0),
        )
        inputs.update(overrides)
        return compute(make_inputs(**inputs), make_params(**params))

    def test_an_expensive_hour_raises_the_published_temperature(self):
        """Spoofing warmer is what tells the pump's curve to back off."""
        result = self._braking()
        assert result.price_adjustment_c > 0
        assert result.price_braking
        assert result.effective_indoor_target_c < result.indoor_target_c

    def test_the_comfort_floor_is_a_hard_stop(self):
        result = self._braking(params={"comfort_min_c": 20.5, "price_comfort_tier": "high"})
        assert result.effective_indoor_target_c >= 20.5
        assert result.price_shift_applied_c == pytest.approx(0.5, abs=1e-6)

    def test_reported_shift_reflects_the_floor_not_the_request(self):
        floored = self._braking(params={"comfort_min_c": 20.0})
        assert floored.price_shift_applied_c == pytest.approx(
            floored.indoor_target_c - floored.effective_indoor_target_c
        )

    def test_aggressive_tiers_sag_further(self):
        low = self._braking(params={"price_comfort_tier": "low"})
        high = self._braking(params={"price_comfort_tier": "high"})
        assert high.price_shift_applied_c > low.price_shift_applied_c

    def test_pre_braking_starts_before_the_spike(self):
        """Braking that starts later than the fall time is still pushing heat
        into the hour it was meant to avoid."""
        result = compute(
            make_inputs(
                current_price=2.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=2),
                fall_minutes=240.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="mid",
                cold_caution="low",
            ),
        )
        assert result.upcoming_spike_in_min == pytest.approx(120.0)
        assert result.price_adjustment_c > 0
        assert "pre-braking" in result.reason

    def test_a_spike_beyond_the_fall_time_is_not_acted_on_yet(self):
        result = compute(
            make_inputs(
                current_price=1.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=10),
                fall_minutes=120.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="mid",
                cold_caution="low",
            ),
        )
        assert result.price_adjustment_c == 0.0

    def test_lead_time_comes_from_the_measured_fall_time(self):
        slab = compute(
            make_inputs(
                current_price=1.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=5),
                fall_minutes=360.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="mid",
                cold_caution="low",
            ),
        )
        assert slab.lead_minutes_effective == 360.0
        assert slab.price_adjustment_c > 0


class TestPreCharge:
    def test_high_tier_banks_heat_while_it_is_cheap(self):
        result = compute(
            make_inputs(
                current_price=1.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=1),
                rise_minutes=120.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        assert result.precharge_active
        assert result.effective_indoor_target_c > result.indoor_target_c
        assert result.price_adjustment_c < 0
        assert "pre-charging" in result.reason

    def test_lower_tiers_do_not_pre_charge(self):
        for tier in ("low", "mid"):
            result = compute(
                make_inputs(
                    current_price=1.0,
                    price_data_available=True,
                    price_forecast=spiky_forecast(spike_at=1),
                    rise_minutes=120.0,
                ),
                make_params(
                    enable_price_compensation=True,
                    price_comfort_tier=tier,
                    cold_caution="low",
                ),
            )
            assert not result.precharge_active

    def test_pre_charge_is_timed_off_the_rise_time(self):
        """Banked heat must have ARRIVED before the spike, not be on its way."""
        result = compute(
            make_inputs(
                current_price=1.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=6),
                rise_minutes=60.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        assert not result.precharge_active

    def test_no_pre_charging_once_the_price_is_already_high(self):
        result = compute(
            make_inputs(
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=1),
                rise_minutes=120.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        assert not result.precharge_active


class TestColdCaution:
    """The one manual control over deep cold: braking is cheap to enter and
    expensive to exit, and on most installs the exit eventually runs through
    resistive backup heat."""

    def test_below_the_floor_braking_stops_entirely(self):
        result = compute(
            make_inputs(
                raw_outdoor_temp_c=-25.0,
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=0),
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        assert result.cold_brake_factor == 0.0
        assert result.price_adjustment_c == 0.0
        assert "too cold to brake" in result.reason

    def test_higher_caution_stops_braking_sooner(self):
        def factor(caution):
            return compute(
                make_inputs(
                    raw_outdoor_temp_c=-8.0,
                    current_price=5.0,
                    price_data_available=True,
                    price_forecast=spiky_forecast(spike_at=0),
                ),
                make_params(
                    enable_price_compensation=True,
                    price_comfort_tier="high",
                    cold_caution=caution,
                ),
            ).cold_brake_factor

        assert factor("high") == 0.0
        assert factor("mid") < factor("low")

    def test_mild_weather_brakes_at_full_authority(self):
        for caution in heuristic.COLD_CAUTIONS:
            result = compute(
                make_inputs(
                    raw_outdoor_temp_c=10.0,
                    current_price=5.0,
                    price_data_available=True,
                    price_forecast=spiky_forecast(spike_at=0),
                ),
                make_params(
                    enable_price_compensation=True,
                    price_comfort_tier="high",
                    cold_caution=caution,
                ),
            )
            assert result.cold_brake_factor == pytest.approx(1.0)

    def test_measured_recovery_limits_the_sag_further(self):
        """Once a band knows how fast it recovers, the sag is limited to what
        can actually be bought back — the question the old hand-drawn taper was
        approximating."""
        def factor(recoverable):
            return heuristic.cold_brake_factor(
                10.0,
                heuristic.resolve_cold_caution("mid"),
                recoverable_sag_c=recoverable,
                tier_max_sag_c=3.0,
            )

        assert factor(0.0) == pytest.approx(1.0)  # no measurement, no taper
        assert factor(3.0) == pytest.approx(1.0)
        assert factor(1.5) == pytest.approx(0.5)
        assert factor(0.15) == pytest.approx(0.05)

    def test_caution_exponent_sharpens_the_feasibility_taper(self):
        gentle = heuristic.cold_brake_factor(
            10.0, heuristic.resolve_cold_caution("low"), 1.5, 3.0
        )
        sharp = heuristic.cold_brake_factor(
            10.0, heuristic.resolve_cold_caution("high"), 1.5, 3.0
        )
        assert sharp < gentle

    def test_unknown_names_fall_back_to_balanced(self):
        assert heuristic.resolve_cold_caution("nonsense") is heuristic.COLD_CAUTIONS["mid"]
        assert heuristic.resolve_cold_caution(None) is heuristic.COLD_CAUTIONS["mid"]
        assert heuristic.resolve_price_tier("nonsense") is heuristic.PRICE_TIERS["mid"]


class TestPriceBrakingFlag:
    """The learner freezes on this. If it were wrong in either direction the
    loop would either fight the price excursion or stop learning for no reason."""

    def test_set_while_deliberately_holding_away_from_target(self):
        result = compute(
            make_inputs(
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=0),
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        assert result.price_braking

    def test_set_while_pre_charging_too(self):
        result = compute(
            make_inputs(
                current_price=1.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=1),
                rise_minutes=120.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        assert result.precharge_active
        assert result.price_braking

    def test_clear_when_price_is_doing_nothing(self):
        assert not compute(make_inputs(), make_params()).price_braking

    def test_clear_on_a_flat_day(self):
        result = compute(
            make_inputs(
                current_price=1.0,
                price_data_available=True,
                price_forecast=flat_forecast(),
            ),
            make_params(enable_price_compensation=True),
        )
        assert not result.price_braking


class TestExplainability:
    def test_reason_is_always_populated(self):
        for inputs in (
            make_inputs(),
            make_inputs(indoor_temp_c=None, indoor_data_available=False),
            make_inputs(raw_outdoor_temp_c=30.0),
            make_inputs(
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=0),
            ),
        ):
            result = compute(inputs, make_params(enable_price_compensation=True))
            assert result.reason
            assert "total" in result.reason or "suppressed" in result.reason

    def test_band_thresholds_are_published_for_troubleshooting(self):
        result = compute(
            make_inputs(
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=0),
            ),
            make_params(enable_price_compensation=True),
        )
        assert result.price_band_start is not None
        assert result.price_band_full is not None
        assert result.price_median is not None
        assert result.price_band_start < result.price_band_full


class TestHeatCurveOffset:
    """`heat_curve_offset_c` is the one piece of the native-curve-offset
    output mode that can be unit-tested without Home Assistant — and the sign
    is exactly the kind of thing this project has gotten wrong before (see
    the lag estimator). A house calling for MORE heat means a more negative
    internal spoof (colder outdoor, per learner.py's "Sign convention") but,
    on the common pump convention, a MORE POSITIVE curve offset.
    """

    heat_curve_offset_c = staticmethod(heuristic.heat_curve_offset_c)

    def test_more_heat_wanted_is_positive_by_default(self):
        # Spoofed colder than raw -> more heat wanted.
        value = self.heat_curve_offset_c(
            compensated_outdoor_temp_c=-2.0, raw_outdoor_temp_c=3.0, invert=False
        )
        assert value == pytest.approx(5.0)

    def test_less_heat_wanted_is_negative_by_default(self):
        value = self.heat_curve_offset_c(
            compensated_outdoor_temp_c=6.0, raw_outdoor_temp_c=3.0, invert=False
        )
        assert value == pytest.approx(-3.0)

    def test_invert_flips_the_sign(self):
        assert self.heat_curve_offset_c(
            compensated_outdoor_temp_c=-2.0, raw_outdoor_temp_c=3.0, invert=True
        ) == pytest.approx(-5.0)

    def test_no_compensation_is_zero_regardless_of_invert(self):
        for invert in (False, True):
            assert self.heat_curve_offset_c(
                compensated_outdoor_temp_c=3.0, raw_outdoor_temp_c=3.0, invert=invert
            ) == pytest.approx(0.0)

    def test_rounds_to_a_whole_number(self):
        # Most pumps' native curve-offset parameter is an integer dial — a
        # fractional delta must round rather than be sent as-is.
        value = self.heat_curve_offset_c(
            compensated_outdoor_temp_c=-2.6, raw_outdoor_temp_c=3.0, invert=False
        )
        assert value == 6
        assert isinstance(value, int)


class TestTodayPriceSpreadAndMedian:
    """`today_price_spread_and_median_c` — the one function both the seasonal
    history and `compute()`'s own braking-band logic read the day's spread
    and median through, so they can never disagree for the same cycle."""

    def test_no_forecast_is_none(self):
        assert heuristic.today_price_spread_and_median_c(None) is None

    def test_too_few_forecast_points_is_none(self):
        assert (
            heuristic.today_price_spread_and_median_c(((0.0, 1.0), (1.0, 9.0)))
            is None
        )

    def test_flat_day_has_zero_spread(self):
        result = heuristic.today_price_spread_and_median_c(flat_forecast(price=2.0))
        assert result == pytest.approx((0.0, 2.0))

    def test_spike_widens_the_spread_around_the_median(self):
        # 23 hours at 1.0, one hour at 5.0: median stays at the ordinary
        # price, peak is the spike, spread is peak minus median.
        result = heuristic.today_price_spread_and_median_c(spiky_forecast())
        assert result == pytest.approx((4.0, 1.0))

    def test_never_negative_even_if_median_exceeded_peak(self):
        # Degenerate/contrived forecast, but the function must not hand back a
        # negative "spread" that would then feed a negative significance ratio.
        result = heuristic.today_price_spread_and_median_c(
            tuple((float(h), 1.0) for h in range(6))
        )
        assert result[0] >= 0.0


class TestPriceSpreadHistoryRollover:
    """`update_price_spread_history` — the pure state-advance step behind the
    seasonal half of `price_significance()`. See `PriceSpreadHistory`'s
    docstring for why spread uses a running MAX per date but median just takes
    the latest reading."""

    def test_cold_start_has_no_history(self):
        history = heuristic.initial_price_spread_history()
        assert history.daily_spreads_c == ()
        assert history.daily_medians_c == ()
        assert history.current_date is None

    def test_first_observation_seeds_the_current_date_without_banking(self):
        history = heuristic.update_price_spread_history(
            heuristic.initial_price_spread_history(), "2026-08-01", 0.2, 0.3
        )
        assert history.current_date == "2026-08-01"
        assert history.current_date_max_spread_c == pytest.approx(0.2)
        assert history.current_date_median_c == pytest.approx(0.3)
        # Nothing completed yet, so nothing banked.
        assert history.daily_spreads_c == ()
        assert history.daily_medians_c == ()

    def test_same_date_spread_only_ever_grows(self):
        history = heuristic.update_price_spread_history(
            heuristic.initial_price_spread_history(), "2026-08-01", 0.2, 0.3
        )
        # A smaller spread later the same day must not erase the earlier max
        # — see the module docstring's "0.249 -> 1.139 SEK/kWh on the same
        # date" field observation for why the running max matters.
        history = heuristic.update_price_spread_history(
            history, "2026-08-01", 0.05, 0.3
        )
        assert history.current_date_max_spread_c == pytest.approx(0.2)

    def test_same_date_median_always_takes_the_latest_reading(self):
        """Unlike the spread, the median is a level estimate, not a range —
        see PriceSpreadHistory's docstring for why "latest wins" is the right
        rule here even though it is the wrong one for spread."""
        history = heuristic.update_price_spread_history(
            heuristic.initial_price_spread_history(), "2026-08-01", 0.2, 0.3
        )
        history = heuristic.update_price_spread_history(
            history, "2026-08-01", 0.05, 0.1
        )
        assert history.current_date_median_c == pytest.approx(0.1)

    def test_no_op_call_returns_the_identical_object(self):
        """A cycle that changes nothing must be a true no-op — same object,
        not just an equal one — so the coordinator can skip a debounced state
        save with a plain identity check rather than a deep comparison."""
        history = heuristic.update_price_spread_history(
            heuristic.initial_price_spread_history(), "2026-08-01", 0.2, 0.3
        )
        same = heuristic.update_price_spread_history(history, "2026-08-01", 0.1, 0.3)
        assert same is history

    def test_date_rollover_banks_the_completed_day(self):
        day_one = heuristic.update_price_spread_history(
            heuristic.initial_price_spread_history(), "2026-08-01", 0.2, 0.3
        )
        day_two = heuristic.update_price_spread_history(
            day_one, "2026-08-02", 0.15, 0.25
        )
        assert day_two.daily_spreads_c == (pytest.approx(0.2),)
        assert day_two.daily_medians_c == (pytest.approx(0.3),)
        assert day_two.current_date == "2026-08-02"
        assert day_two.current_date_max_spread_c == pytest.approx(0.15)
        assert day_two.current_date_median_c == pytest.approx(0.25)

    def test_history_window_trims_to_the_newest_days(self):
        history = heuristic.initial_price_spread_history()
        # The first call only SEEDS the current date rather than banking
        # anything (there is no prior day yet to bank), so getting one more
        # BANKED day than the window holds needs PRICE_SPREAD_HISTORY_DAYS + 2
        # total calls. Plain sequential labels rather than real calendar
        # dates: `update_price_spread_history` only ever compares
        # `local_date` for equality, never parses it (see its docstring).
        for day in range(heuristic.PRICE_SPREAD_HISTORY_DAYS + 2):
            history = heuristic.update_price_spread_history(
                history, f"day-{day:03d}", float(day), 0.3
            )
        assert len(history.daily_spreads_c) == heuristic.PRICE_SPREAD_HISTORY_DAYS
        # Oldest first: day 0's spread (banked first) was pushed out, so the
        # oldest surviving entry is day 1's.
        assert history.daily_spreads_c[0] == pytest.approx(1.0)
        assert history.daily_spreads_c[-1] == pytest.approx(
            float(heuristic.PRICE_SPREAD_HISTORY_DAYS)
        )


class TestPriceSignificance:
    """`price_significance` — the combined taper that replaced a
    relative-only spread ratio which ranked days backwards in money terms and
    degenerated near a zero median (see the module docstring's field data)."""

    def test_cold_start_relative_term_contributes_no_damping(self):
        """Fewer than PRICE_SIGNIFICANCE_COLD_START_DAYS stored days: the
        relative term must not block saving while history accumulates, so
        only the absolute floor decides."""
        history = heuristic.initial_price_spread_history()
        significance, reference = heuristic.price_significance(
            today_spread_c=0.05, today_median_c=0.3, history=history, floor_setting_c=1.0
        )
        assert reference is None
        # relative=1.0, absolute=clamp(0.05/1.0)=0.05 -> combined is the min.
        assert significance == pytest.approx(0.05)

    def test_seasonal_reference_is_the_median_of_stored_spreads(self):
        history = heuristic.PriceSpreadHistory(
            daily_spreads_c=(0.1, 0.2, 0.3, 0.4, 0.5),
            daily_medians_c=(0.3,) * 5,
        )
        significance, reference = heuristic.price_significance(
            today_spread_c=0.15,
            today_median_c=0.3,
            history=history,
            # Tiny floor so the absolute term never binds in this test —
            # isolates the relative term's own behaviour.
            floor_setting_c=0.0001,
        )
        assert reference == pytest.approx(0.3)
        assert significance == pytest.approx(0.5)  # 0.15 / 0.3

    def test_median_reference_ignores_one_spike_day(self):
        """Median, not mean — a single outlier day must not drag the
        reference far off what most days actually looked like."""
        normal_days = (0.1, 0.1, 0.1, 0.1, 0.1)
        history_with_spike = heuristic.PriceSpreadHistory(
            daily_spreads_c=normal_days + (50.0,),
            daily_medians_c=(0.3,) * 6,
        )
        _, reference = heuristic.price_significance(
            today_spread_c=0.1,
            today_median_c=0.3,
            history=history_with_spike,
            floor_setting_c=0.0001,
        )
        assert reference == pytest.approx(0.1)

    def test_relative_term_clamps_at_one(self):
        history = heuristic.PriceSpreadHistory(
            daily_spreads_c=(0.1,) * 5, daily_medians_c=(0.3,) * 5
        )
        significance, _ = heuristic.price_significance(
            today_spread_c=10.0,
            today_median_c=0.3,
            history=history,
            floor_setting_c=0.0001,
        )
        assert significance == pytest.approx(1.0)

    def test_explicit_floor_overrides_auto(self):
        history = heuristic.PriceSpreadHistory(
            daily_spreads_c=(0.05,) * 5, daily_medians_c=(0.9,) * 5
        )
        # Auto floor here would be 0.33 * 0.9 = 0.297; an explicit floor of
        # 0.05 must be used verbatim instead, in the price sensor's own units.
        significance, _ = heuristic.price_significance(
            today_spread_c=0.025,
            today_median_c=0.9,
            history=history,
            floor_setting_c=0.05,
        )
        # relative = 0.025 / 0.05 (reference spread) = 0.5
        # absolute = 0.025 / 0.05 (explicit floor) = 0.5
        assert significance == pytest.approx(0.5)

    def test_auto_floor_uses_the_median_of_stored_daily_prices(self):
        history = heuristic.PriceSpreadHistory(
            # Tiny reference spreads so the relative term clamps to 1.0 and
            # only the absolute (auto-floor) term is what this test measures.
            daily_spreads_c=(0.001,) * 5,
            daily_medians_c=(0.2, 0.3, 0.4, 0.5, 0.6),
        )
        # Auto floor = 0.33 * median(0.2..0.6) = 0.33 * 0.4 = 0.132.
        significance, _ = heuristic.price_significance(
            today_spread_c=0.066,  # half the auto floor
            today_median_c=999.0,  # must be ignored: history exists
            history=history,
            floor_setting_c=0.0,
        )
        assert significance == pytest.approx(0.5, rel=1e-3)

    def test_auto_floor_falls_back_to_todays_median_with_no_history(self):
        history = heuristic.initial_price_spread_history()
        # Auto floor = 0.33 * today's own median (0.3) = 0.099.
        significance, _ = heuristic.price_significance(
            today_spread_c=0.0495,  # half the auto floor
            today_median_c=0.3,
            history=history,
            floor_setting_c=0.0,
        )
        assert significance == pytest.approx(0.5, rel=1e-3)

    def test_absolute_floor_backstops_a_near_zero_median_day(self):
        """The exact degeneracy the module docstring's field data describes:
        a 0.001/0.01 median/peak pair scores a 9.0 RELATIVE ratio (full
        authority), but a tiny absolute spread must still be tapered near 0
        once a real floor backstops it."""
        history = heuristic.PriceSpreadHistory(
            daily_spreads_c=(0.05,) * 5, daily_medians_c=(0.05,) * 5
        )
        significance, _ = heuristic.price_significance(
            today_spread_c=0.009,  # 0.01 peak - 0.001 median
            today_median_c=0.001,
            history=history,
            floor_setting_c=0.10,
        )
        assert significance < 0.1

    def test_either_term_being_low_is_enough_to_damp(self):
        """min(), not a product or an average: a day that is significant on
        ONE axis but not the other must still be damped."""
        history = heuristic.PriceSpreadHistory(
            daily_spreads_c=(0.1,) * 5, daily_medians_c=(0.3,) * 5
        )
        # High relative (spread far exceeds the seasonal reference) but low
        # absolute (still under an explicit high floor).
        significance, _ = heuristic.price_significance(
            today_spread_c=1.0, today_median_c=0.3, history=history, floor_setting_c=50.0
        )
        assert significance == pytest.approx(1.0 / 50.0)


class TestComputeAppliesSignificanceTaper:
    """Integration: `compute()` must apply `price_significance_factor` to
    BOTH the braking and pre-charge branches, as a continuous taper rather
    than a second hard gate — see the module docstring's "Price significance:
    a taper, not a gate" section."""

    def test_default_factor_of_one_changes_nothing(self):
        """Every pre-existing price test in this file constructs inputs
        without a significance factor and relies on it defaulting to 1.0 (no
        damping) — this pins that default down explicitly."""
        baseline = compute(
            make_inputs(
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=0),
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        damped = compute(
            make_inputs(
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=0),
                price_significance_factor=1.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        assert damped.price_adjustment_c == pytest.approx(baseline.price_adjustment_c)

    def test_half_significance_halves_the_braking_shift(self):
        full = compute(
            make_inputs(
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=0),
                price_significance_factor=1.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        half = compute(
            make_inputs(
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=0),
                price_significance_factor=0.5,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        assert half.price_shift_applied_c == pytest.approx(
            full.price_shift_applied_c * 0.5
        )

    def test_zero_significance_fully_suppresses_braking(self):
        result = compute(
            make_inputs(
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=0),
                price_significance_factor=0.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        assert result.price_adjustment_c == 0.0
        assert not result.price_braking

    def test_zero_significance_also_suppresses_pre_charge(self):
        result = compute(
            make_inputs(
                current_price=1.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=1),
                rise_minutes=120.0,
                price_significance_factor=0.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        # Pre-charge is still the branch that WOULD have run (the price is
        # cheap with a spike coming), it just banks nothing once damped.
        assert result.precharge_active
        assert result.price_adjustment_c == 0.0

    def test_half_significance_halves_pre_charge_too(self):
        full = compute(
            make_inputs(
                current_price=1.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=1),
                rise_minutes=120.0,
                price_significance_factor=1.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        half = compute(
            make_inputs(
                current_price=1.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=1),
                rise_minutes=120.0,
                price_significance_factor=0.5,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        assert half.price_shift_applied_c == pytest.approx(
            full.price_shift_applied_c * 0.5
        )

    def test_significance_fields_are_echoed_onto_the_result(self):
        result = compute(
            make_inputs(
                price_significance_factor=0.42,
                today_price_spread_c=1.23,
                seasonal_reference_spread_c=0.87,
            ),
            make_params(),
        )
        assert result.price_significance_factor == pytest.approx(0.42)
        assert result.today_price_spread_c == pytest.approx(1.23)
        assert result.seasonal_reference_spread_c == pytest.approx(0.87)

    def test_significance_fields_survive_the_heating_cutoff(self):
        """Informational, like current_price — a house above the cutoff can
        still see how significant today's price swing is."""
        result = compute(
            make_inputs(
                raw_outdoor_temp_c=18.0,
                price_significance_factor=0.42,
                today_price_spread_c=1.23,
                seasonal_reference_spread_c=0.87,
            ),
            make_params(heating_cutoff_c=18.0),
        )
        assert result.heating_cutoff_engaged
        assert result.price_significance_factor == pytest.approx(0.42)
        assert result.today_price_spread_c == pytest.approx(1.23)
        assert result.seasonal_reference_spread_c == pytest.approx(0.87)
