"""Guards the config-entry allow-list used by `async_migrate_entry`.

The migration prunes anything outside `KNOWN_CONFIG_KEYS`, which makes that set
load-bearing in one dangerous direction: a setting added to the flow but not to
the set is silently deleted from every entry on the next version bump, and the
user's choice reverts to a default with nothing to show why.

`test_allow_list_covers_every_live_conf_key` is what catches that. It works
because `const.py` defines a `CONF_*` constant for exactly the keys the code
still reads — the pre-v0.2.0 ones were removed with the code that read them —
so the constants are an independent statement of the same fact, derived from
the module rather than restated here.

`homeassistant` is never installed in CI, so this tests the pruning rule
against `const.py` (which is import-free by design) rather than calling the
migration itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from loader import load  # noqa: E402

const = load("const")


def prune(entry: dict) -> dict:
    """The migration's rule, applied to one of `data`/`options`."""
    return {k: v for k, v in entry.items() if k in const.KNOWN_CONFIG_KEYS}


def test_allow_list_covers_every_live_conf_key() -> None:
    live = {
        value
        for name, value in vars(const).items()
        if name.startswith("CONF_") and isinstance(value, str)
    }
    assert live - const.KNOWN_CONFIG_KEYS == set()


def test_allow_list_has_no_keys_the_code_stopped_reading() -> None:
    # The mirror of the test above: a key left in the set after its CONF_*
    # constant is deleted would quietly survive every future migration.
    # `name` is the exception — it is homeassistant.const.CONF_NAME, inlined
    # because const.py must not import Home Assistant.
    live = {
        value
        for name, value in vars(const).items()
        if name.startswith("CONF_") and isinstance(value, str)
    }
    assert const.KNOWN_CONFIG_KEYS - live == {"name"}


# The real options blob from entry 01KY73HX7YP2QZ5440KBCTTZ80 on 2026-08-12,
# an install carried across the v0.2.0 rewrite. Verbatim rather than
# hand-shortened, because the point is to prove the rule against what actually
# accumulates in the field.
LEGACY_OPTIONS = {
    "cold_taper_full_c": -20.0,
    "cold_taper_min_factor": 0.4,
    "cold_taper_start_c": -10.0,
    "comfort_max_c": 23.0,
    "comfort_min_c": 18.0,
    "enable_data_logging": True,
    "enable_price_compensation": False,
    "enable_solar_input": False,
    "enable_wind_input": False,
    "enable_wind_rc": False,
    "heating_cutoff_c": 18.0,
    "k_indoor": 1.5,
    "k_price": 5.0,
    "k_sun": 3.0,
    "k_wind": 0.3,
    "mpc_horizon_hours": 24.0,
    "mpc_max_heating_delta_c": 8.0,
    "mpc_min_confidence": 1.0,
    "nordpool_price_entity": "sensor.nordpool_kwh_se2_sek_3_06_025",
    "ohmonwifi_host": "ohmonwifiplus.local",
    "output_number_entity": "number.nibe_ohmigo_temperature",
    "power_sensor": "sensor.nibe_electricity_usage",
    "precharge_max_boost_c": 1.0,
    "price_max_drop_c": 1.0,
    "price_threshold_max": 3.0,
    "price_threshold_start": 1.5,
    "rc_wind_reference_ms": 5.0,
    "update_interval_minutes": 15.0,
    "weather_entity": "weather.smhi_home",
}


def test_prunes_a_real_pre_rewrite_entry() -> None:
    assert prune(LEGACY_OPTIONS) == {
        "comfort_min_c": 18.0,
        "enable_data_logging": True,
        "enable_price_compensation": False,
        "enable_solar_input": False,
        "enable_wind_input": False,
        "nordpool_price_entity": "sensor.nordpool_kwh_se2_sek_3_06_025",
        "ohmonwifi_host": "ohmonwifiplus.local",
        "output_number_entity": "number.nibe_ohmigo_temperature",
        "power_sensor": "sensor.nibe_electricity_usage",
        "weather_entity": "weather.smhi_home",
    }


def test_keeps_every_user_choice_it_still_understands() -> None:
    # Pruning must never change a surviving value, only drop whole keys —
    # the settings above are someone's deliberate choices.
    pruned = prune(LEGACY_OPTIONS)
    assert all(LEGACY_OPTIONS[k] == v for k, v in pruned.items())


def test_is_idempotent() -> None:
    assert prune(prune(LEGACY_OPTIONS)) == prune(LEGACY_OPTIONS)


def test_data_bucket_survives_intact() -> None:
    # Setup data from the same entry. `weather_entity` and
    # `nordpool_price_entity` live here as well as in options because they
    # moved between the two buckets; `_entry_value` layers options over data,
    # so both copies are live and neither may be pruned.
    data = {
        "indoor_target_temperature": 20.0,
        "indoor_temp_sensor": "sensor.indoor_indoor_indoor_kallare_temperature",
        "name": "TrueTemp",
        "nordpool_price_entity": "sensor.nordpool_kwh_se2_sek_3_06_025",
        "outdoor_temp_sensor": "sensor.indoor_indoor_indoor_north_side_temperature",
        "weather_entity": "weather.smhi_home",
    }
    assert prune(data) == data
