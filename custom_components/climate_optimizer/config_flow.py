"""Config flow for ClimateOptimizer.

Initial setup only asks for entity selections, the indoor target temperature
and the heating-system type. Every tunable coefficient lives in the options
flow so it can be changed later without deleting and re-adding the integration.

The options flow is a MENU of small, single-purpose pages rather than one long
form. Each page saves independently: submitting "Comfort" writes only the
comfort keys and leaves everything else untouched (see `_save`), so pages can be
edited in any order without one page's stale defaults overwriting another's
freshly-saved values.
"""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .autotune import HEATING_TYPE_RADIATORS, HEATING_TYPE_UNDERFLOOR, TUNING_MODES
from .const import (
    CONF_COMFORT_MAX_C,
    CONF_COMFORT_MIN_C,
    CONF_ENABLE_DATA_LOGGING,
    CONF_ENABLE_PRICE_COMPENSATION,
    CONF_ENABLE_SOLAR_INPUT,
    CONF_ENABLE_WIND_INPUT,
    CONF_ENABLE_WIND_RC,
    CONF_HEATING_CUTOFF_C,
    CONF_HEATING_TYPE,
    CONF_COLD_TAPER_FULL_C,
    CONF_COLD_TAPER_MIN_FACTOR,
    CONF_COLD_TAPER_START_C,
    CONF_INDOOR_TARGET_TEMPERATURE,
    CONF_INDOOR_TEMP_SENSOR,
    CONF_K_INDOOR,
    CONF_K_PRICE,
    CONF_K_SUN,
    CONF_K_WIND,
    CONF_MPC_HORIZON_HOURS,
    CONF_MPC_MAX_HEATING_DELTA_C,
    CONF_MPC_MIN_CONFIDENCE,
    CONF_NORDPOOL_PRICE_ENTITY,
    CONF_OHMONWIFI_HOST,
    CONF_OUTDOOR_TEMP_SENSOR,
    CONF_OUTPUT_NUMBER_ENTITY,
    CONF_POWER_SENSOR,
    CONF_PRECHARGE_MAX_BOOST_C,
    CONF_PRICE_MAX_DROP_C,
    CONF_PRICE_THRESHOLD_MAX,
    CONF_PRICE_THRESHOLD_START,
    CONF_RC_WIND_REFERENCE_MS,
    CONF_TUNING_MODE,
    CONF_UPDATE_INTERVAL_MINUTES,
    CONF_WEATHER_ENTITY,
    DEFAULT_COMFORT_MAX_C,
    DEFAULT_COMFORT_MIN_C,
    DEFAULT_ENABLE_DATA_LOGGING,
    DEFAULT_ENABLE_PRICE_COMPENSATION,
    DEFAULT_ENABLE_SOLAR_INPUT,
    DEFAULT_ENABLE_WIND_INPUT,
    DEFAULT_ENABLE_WIND_RC,
    DEFAULT_COLD_TAPER_FULL_C,
    DEFAULT_COLD_TAPER_MIN_FACTOR,
    DEFAULT_COLD_TAPER_START_C,
    DEFAULT_HEATING_CUTOFF_C,
    DEFAULT_HEATING_TYPE,
    DEFAULT_INDOOR_TARGET_TEMPERATURE,
    DEFAULT_K_INDOOR,
    DEFAULT_K_PRICE,
    DEFAULT_K_SUN,
    DEFAULT_K_WIND,
    DEFAULT_MPC_HORIZON_HOURS,
    DEFAULT_MPC_MAX_HEATING_DELTA_C,
    DEFAULT_MPC_MIN_CONFIDENCE,
    DEFAULT_PRECHARGE_MAX_BOOST_C,
    DEFAULT_PRICE_MAX_DROP_C,
    DEFAULT_PRICE_THRESHOLD_MAX,
    DEFAULT_PRICE_THRESHOLD_START,
    DEFAULT_RC_WIND_REFERENCE_MS,
    DEFAULT_TUNING_MODE,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
)


# Options-flow section keys for the two optional "push the value out"
# targets, purely a UI grouping concern (gives each its own header) — the
# entry's stored options stay flat under CONF_OUTPUT_NUMBER_ENTITY /
# CONF_OHMONWIFI_HOST regardless, so coordinator.py never needs to know
# sections exist at all (see async_step_init, which flattens on submit).
SECTION_OUTPUT_NUMBER = "output_number_push"
SECTION_OHMONWIFI = "ohmonwifi_direct"

# Timeout for validating an OhmOnWifi host at options-save time. Short but
# a bit more generous than the coordinator's own per-cycle push timeout
# (coordinator.OHMONWIFI_REQUEST_TIMEOUT_SECONDS), since this happens once,
# interactively, while the user is watching the form.
OHMONWIFI_VALIDATE_TIMEOUT_SECONDS = 5.0


async def _async_ohmonwifi_reachable(hass: HomeAssistant, host: str) -> bool:
    """Best-effort reachability check against an OhmOnWifi/Ohmigo device's
    own `/info` endpoint (see the vendor's published API doc), used to
    validate the optional direct-push host at options-save time so a typo'd
    hostname/IP doesn't silently do nothing every cycle instead of erroring
    where the user can see it."""
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            f"http://{host}/info",
            timeout=aiohttp.ClientTimeout(total=OHMONWIFI_VALIDATE_TIMEOUT_SECONDS),
        ) as response:
            response.raise_for_status()
    except Exception:  # noqa: BLE001 - any failure just means "not reachable"
        return False
    return True


def _user_data_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "ClimateOptimizer")): str,
            vol.Required(
                CONF_INDOOR_TEMP_SENSOR, default=defaults.get(CONF_INDOOR_TEMP_SENSOR)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Required(
                CONF_OUTDOOR_TEMP_SENSOR, default=defaults.get(CONF_OUTDOOR_TEMP_SENSOR)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            # Optional now that the solar and wind terms can each be switched
            # off: the weather entity's only consumers are those two, so an
            # install using neither has nothing to point it at.
            vol.Optional(
                CONF_WEATHER_ENTITY, default=defaults.get(CONF_WEATHER_ENTITY)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="weather")),
            vol.Optional(
                CONF_NORDPOOL_PRICE_ENTITY, default=defaults.get(CONF_NORDPOOL_PRICE_ENTITY)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Required(
                CONF_INDOOR_TARGET_TEMPERATURE,
                default=defaults.get(
                    CONF_INDOOR_TARGET_TEMPERATURE, DEFAULT_INDOOR_TARGET_TEMPERATURE
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=10, max=30, step=0.5, unit_of_measurement="°C", mode="box"
                )
            ),
            # Asked at setup rather than buried in options because it is a
            # property of the installation that never changes, and because
            # auto-tuning needs it from the very first cycle: it is the only
            # source of the emitter dead-time floor on the derived closed-loop
            # response (see autotune.py).
            vol.Required(
                CONF_HEATING_TYPE,
                default=defaults.get(CONF_HEATING_TYPE, DEFAULT_HEATING_TYPE),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[HEATING_TYPE_UNDERFLOOR, HEATING_TYPE_RADIATORS],
                    translation_key="heating_type",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


class ClimateOptimizerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle initial setup of a ClimateOptimizer zone."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_INDOOR_TEMP_SENSOR])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=_user_data_schema(), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_INDOOR_TEMP_SENSOR] != entry.unique_id:
                await self.async_set_unique_id(user_input[CONF_INDOOR_TEMP_SENSOR])
                self._abort_if_unique_id_configured()
            return self.async_update_reload_and_abort(
                entry, title=user_input[CONF_NAME], data=user_input
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_user_data_schema(dict(entry.data)),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> ClimateOptimizerOptionsFlow:
        return ClimateOptimizerOptionsFlow()


class ClimateOptimizerOptionsFlow(config_entries.OptionsFlow):
    """All tunable coefficients and toggles, editable without reinstalling.

    Organised as a menu of focused pages. Each page's submit handler calls
    `_save`, which merges that page's keys over the *currently stored* options
    and writes the result — deliberately NOT `async_create_entry(data=user_input)`
    with only one page's fields, which would drop every key the page does not
    contain.
    """

    def _current(self) -> dict[str, Any]:
        """Effective config: stored options layered over the entry's setup data,
        which is what every page shows as its defaults."""
        return {**self.config_entry.data, **self.config_entry.options}

    def _save(self, updates: dict[str, Any]) -> config_entries.ConfigFlowResult:
        """Persist one page's fields, preserving every other stored option."""
        merged = {**self.config_entry.options, **updates}
        return self.async_create_entry(data=merged)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "sensors",
                "comfort",
                "tuning",
                "price",
                "output",
                "advanced",
            ],
        )

    # --- Page: sensors & inputs ---------------------------------------------

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return self._save(user_input)

        current = self._current()
        return self.async_show_form(
            step_id="sensors",
            data_schema=vol.Schema(
                {
                    # Optional: only the wind and solar terms consume it, and
                    # both can be switched off below.
                    vol.Optional(
                        CONF_WEATHER_ENTITY,
                        default=current.get(CONF_WEATHER_ENTITY),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="weather")
                    ),
                    vol.Optional(
                        CONF_NORDPOOL_PRICE_ENTITY,
                        default=current.get(CONF_NORDPOOL_PRICE_ENTITY),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    ),
                    # Optional: enables real cost/energy figures in the local
                    # history log (see data_logger.py) and a diagnostic power
                    # echo sensor. Not restricted to device_class "power" since
                    # not every power integration sets it, same reasoning as the
                    # price entity above.
                    vol.Optional(
                        CONF_POWER_SENSOR,
                        default=current.get(CONF_POWER_SENSOR),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    ),
                    # Each toggle switches its term off entirely — in the
                    # heuristic AND in the thermal model, which then stops
                    # carrying a dimension it will never see excitation on (see
                    # rc_model.py). Not the same as setting the coefficient to
                    # zero, which would leave that dimension in place.
                    vol.Required(
                        CONF_ENABLE_SOLAR_INPUT,
                        default=current.get(
                            CONF_ENABLE_SOLAR_INPUT, DEFAULT_ENABLE_SOLAR_INPUT
                        ),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_ENABLE_WIND_INPUT,
                        default=current.get(
                            CONF_ENABLE_WIND_INPUT, DEFAULT_ENABLE_WIND_INPUT
                        ),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_UPDATE_INTERVAL_MINUTES,
                        default=current.get(
                            CONF_UPDATE_INTERVAL_MINUTES,
                            DEFAULT_UPDATE_INTERVAL_MINUTES,
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=5, max=60, step=1, unit_of_measurement="min", mode="box"
                        )
                    ),
                }
            ),
        )

    # --- Page: comfort -------------------------------------------------------

    async def async_step_comfort(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return self._save(user_input)

        # Everything on this page is genuine occupant preference — nothing here
        # is derivable from the house, so none of it is ever auto-tuned. The
        # indoor target itself is deliberately absent: it's a number.* entity
        # (see number.py) so automations can move it without reloading the entry.
        current = self._current()
        return self.async_show_form(
            step_id="comfort",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_COMFORT_MIN_C,
                        default=current.get(CONF_COMFORT_MIN_C, DEFAULT_COMFORT_MIN_C),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=5, max=30, step=0.5, unit_of_measurement="°C", mode="box"
                        )
                    ),
                    vol.Required(
                        CONF_COMFORT_MAX_C,
                        default=current.get(CONF_COMFORT_MAX_C, DEFAULT_COMFORT_MAX_C),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=5, max=30, step=0.5, unit_of_measurement="°C", mode="box"
                        )
                    ),
                    vol.Required(
                        CONF_HEATING_CUTOFF_C,
                        default=current.get(
                            CONF_HEATING_CUTOFF_C, DEFAULT_HEATING_CUTOFF_C
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=5, max=25, step=0.5, unit_of_measurement="°C", mode="box"
                        )
                    ),
                }
            ),
        )

    # --- Page: tuning --------------------------------------------------------

    async def async_step_tuning(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return self._save(user_input)

        # The mode field here only SEEDS the live `select.*_tuning_mode` entity
        # on first run; the select is the source of truth thereafter, so
        # changing it here on an established install will not move the switch.
        # The k_* values below are not dead weight in Auto mode: they are what
        # the derivation blends out of while evidence accumulates, and what it
        # falls back to whenever the model fails a trust gate.
        current = self._current()
        return self.async_show_form(
            step_id="tuning",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TUNING_MODE,
                        default=current.get(CONF_TUNING_MODE, DEFAULT_TUNING_MODE),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=list(TUNING_MODES),
                            translation_key="tuning_mode",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        CONF_HEATING_TYPE,
                        default=current.get(CONF_HEATING_TYPE, DEFAULT_HEATING_TYPE),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[HEATING_TYPE_UNDERFLOOR, HEATING_TYPE_RADIATORS],
                            translation_key="heating_type",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        CONF_K_INDOOR,
                        default=current.get(CONF_K_INDOOR, DEFAULT_K_INDOOR),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=10, step=0.1, mode="box"
                        )
                    ),
                    vol.Required(
                        CONF_K_WIND, default=current.get(CONF_K_WIND, DEFAULT_K_WIND)
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=3, step=0.05, mode="box"
                        )
                    ),
                    vol.Required(
                        CONF_K_SUN, default=current.get(CONF_K_SUN, DEFAULT_K_SUN)
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=10, step=0.1, mode="box"
                        )
                    ),
                }
            ),
        )

    # --- Page: price / savings ----------------------------------------------

    async def async_step_price(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return self._save(user_input)

        # The active tier (Low/Mid/High) is a live entity (select.py), not an
        # option — it's flipped too often to justify an entry reload. The
        # absolute price thresholds are only a fallback for when no day-ahead
        # forecast is available; with one, the band is derived from the day's
        # own distribution (see heuristic._price_band_info). The three
        # cold-taper values are likewise only used in Manual mode — Auto
        # replaces them with a recovery-feasibility factor (autotune.py).
        current = self._current()
        return self.async_show_form(
            step_id="price",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ENABLE_PRICE_COMPENSATION,
                        default=current.get(
                            CONF_ENABLE_PRICE_COMPENSATION,
                            DEFAULT_ENABLE_PRICE_COMPENSATION,
                        ),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_K_PRICE,
                        default=current.get(CONF_K_PRICE, DEFAULT_K_PRICE),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=15, step=0.1, mode="box"
                        )
                    ),
                    vol.Required(
                        CONF_PRECHARGE_MAX_BOOST_C,
                        default=current.get(
                            CONF_PRECHARGE_MAX_BOOST_C, DEFAULT_PRECHARGE_MAX_BOOST_C
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=5, step=0.1, unit_of_measurement="°C", mode="box"
                        )
                    ),
                    vol.Required(
                        CONF_PRICE_THRESHOLD_START,
                        default=current.get(
                            CONF_PRICE_THRESHOLD_START, DEFAULT_PRICE_THRESHOLD_START
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=20, step=0.1, mode="box"
                        )
                    ),
                    vol.Required(
                        CONF_PRICE_THRESHOLD_MAX,
                        default=current.get(
                            CONF_PRICE_THRESHOLD_MAX, DEFAULT_PRICE_THRESHOLD_MAX
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=20, step=0.1, mode="box"
                        )
                    ),
                    vol.Required(
                        CONF_COLD_TAPER_START_C,
                        default=current.get(
                            CONF_COLD_TAPER_START_C, DEFAULT_COLD_TAPER_START_C
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=-30, max=10, step=1, unit_of_measurement="°C", mode="box"
                        )
                    ),
                    vol.Required(
                        CONF_COLD_TAPER_FULL_C,
                        default=current.get(
                            CONF_COLD_TAPER_FULL_C, DEFAULT_COLD_TAPER_FULL_C
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=-40, max=5, step=1, unit_of_measurement="°C", mode="box"
                        )
                    ),
                    vol.Required(
                        CONF_COLD_TAPER_MIN_FACTOR,
                        default=current.get(
                            CONF_COLD_TAPER_MIN_FACTOR, DEFAULT_COLD_TAPER_MIN_FACTOR
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0, max=1, step=0.05, mode="box"
                        )
                    ),
                }
            ),
        )

    # --- Page: output push ---------------------------------------------------

    async def async_step_output(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        current = self._current()

        if user_input is not None:
            # The two push targets are grouped into headed sections purely for
            # the UI; flatten them straight back into plain top-level keys so
            # the entry's stored options — and coordinator.py/const.py, which
            # only know CONF_OUTPUT_NUMBER_ENTITY/CONF_OHMONWIFI_HOST as flat
            # keys — never need to know sections exist.
            output_number_data = dict(user_input.pop(SECTION_OUTPUT_NUMBER, {}))
            ohmonwifi_data = dict(user_input.pop(SECTION_OHMONWIFI, {}))
            # Normalize a blank text field to None so an untouched/cleared host
            # consistently reads as "not configured" (see
            # coordinator.ohmonwifi_host's `or None`).
            host = (ohmonwifi_data.get(CONF_OHMONWIFI_HOST) or "").strip()
            user_input[CONF_OUTPUT_NUMBER_ENTITY] = output_number_data.get(
                CONF_OUTPUT_NUMBER_ENTITY
            )
            user_input[CONF_OHMONWIFI_HOST] = host or None
            if host and not await _async_ohmonwifi_reachable(self.hass, host):
                errors["base"] = "cannot_connect"
                # Re-show the form with what was just submitted (not the
                # previously-saved options) so the failed field is easy to spot
                # and everything else the user typed isn't lost.
                current = {**current, **user_input}
            else:
                return self._save(user_input)

        return self.async_show_form(
            step_id="output",
            errors=errors,
            data_schema=vol.Schema(
                {
                    # Both targets are independent, not alternatives — set one,
                    # both, or neither. See README's "Optional: push the value
                    # automatically".
                    vol.Required(SECTION_OUTPUT_NUMBER): section(
                        vol.Schema(
                            {
                                # Pushes the published compensated outdoor
                                # temperature into another integration's number
                                # entity (e.g. a heat pump's virtual/AUX
                                # outdoor-temperature input such as
                                # `number.nibe_ohmigo_temperature` with
                                # OhmOnWifi/OhmigoWifi set up as a HA entity).
                                # Off by default. Mirrors exactly what the main
                                # sensor publishes (raw while in learn mode,
                                # compensated once the activation switch is on).
                                vol.Optional(
                                    CONF_OUTPUT_NUMBER_ENTITY,
                                    default=current.get(CONF_OUTPUT_NUMBER_ENTITY),
                                ): selector.EntitySelector(
                                    selector.EntitySelectorConfig(domain="number")
                                ),
                            }
                        ),
                        {"collapsed": False},
                    ),
                    vol.Required(SECTION_OHMONWIFI): section(
                        vol.Schema(
                            {
                                # In addition to (not instead of) the section
                                # above: talks directly to an OhmOnWifi/Ohmigo
                                # device's own local HTTP API
                                # (http://<host>/AT/?T=<value>, per its published
                                # API doc), no HA entity in between. Unset by
                                # default; validated with a live reachability
                                # check against the device's own /info endpoint
                                # before the save is accepted.
                                vol.Optional(
                                    CONF_OHMONWIFI_HOST,
                                    default=current.get(CONF_OHMONWIFI_HOST),
                                ): vol.Any(
                                    None,
                                    selector.TextSelector(
                                        selector.TextSelectorConfig(
                                            type=selector.TextSelectorType.TEXT
                                        )
                                    ),
                                ),
                            }
                        ),
                        {"collapsed": False},
                    ),
                }
            ),
        )

    # --- Page: advanced / experimental ---------------------------------------

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return self._save(user_input)

        # The RC wind term is off by default and additionally gated on the wind
        # input being on at all: it's only worth enabling for houses expected to
        # be genuinely wind-sensitive (old, leaky, exposed), because the wind
        # regressor is collinear with the envelope term and for a typical house
        # just adds estimation noise. See rc_model.py's module docstring.
        # MPC remains advisory/shadow-only — these never affect the published
        # compensated temperature, only the MPC diagnostic sensors.
        current = self._current()
        return self.async_show_form(
            step_id="advanced",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ENABLE_WIND_RC,
                        default=current.get(
                            CONF_ENABLE_WIND_RC, DEFAULT_ENABLE_WIND_RC
                        ),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_RC_WIND_REFERENCE_MS,
                        default=current.get(
                            CONF_RC_WIND_REFERENCE_MS, DEFAULT_RC_WIND_REFERENCE_MS
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1, max=15, step=0.5, unit_of_measurement="m/s", mode="box"
                        )
                    ),
                    vol.Required(
                        CONF_MPC_HORIZON_HOURS,
                        default=current.get(
                            CONF_MPC_HORIZON_HOURS, DEFAULT_MPC_HORIZON_HOURS
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=6, max=48, step=1, unit_of_measurement="h", mode="box"
                        )
                    ),
                    vol.Required(
                        CONF_MPC_MAX_HEATING_DELTA_C,
                        default=current.get(
                            CONF_MPC_MAX_HEATING_DELTA_C,
                            DEFAULT_MPC_MAX_HEATING_DELTA_C,
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1, max=20, step=0.5, unit_of_measurement="°C", mode="box"
                        )
                    ),
                    vol.Required(
                        CONF_MPC_MIN_CONFIDENCE,
                        default=current.get(
                            CONF_MPC_MIN_CONFIDENCE, DEFAULT_MPC_MIN_CONFIDENCE
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=0, max=1, step=0.05, mode="box")
                    ),
                    # Opt-in local history log (data_logger.py) for offline
                    # model testing later — purely local, off by default.
                    vol.Required(
                        CONF_ENABLE_DATA_LOGGING,
                        default=current.get(
                            CONF_ENABLE_DATA_LOGGING, DEFAULT_ENABLE_DATA_LOGGING
                        ),
                    ): selector.BooleanSelector(),
                }
            ),
        )
