"""Config flow for TrueTemp.

Setup asks four required questions and a name, plus two optional entities
(weather forecast, electricity price) also editable later. Options add four
small pages: the building's own sensors and logging, optional sources, one
comfort preference, and plumbing — the output page branches into a second,
mode-specific page once a method is chosen.

The building-facing fields (indoor/outdoor sensor, heating type) are asked at
setup *and* reachable again from the options "Settings" page, deliberately
duplicating what `async_step_reconfigure` also offers — most occupants only
ever find the gear-icon Configure button, not the entry's separate Reconfigure
action, so the fields that matter most need to live where they'll look.

Nothing here is a control gain. `k_indoor`, `k_wind`, `k_sun`, `k_price`, three
cold-taper thresholds, two price thresholds, a pre-charge boost, three MPC
settings, an update interval, an upper comfort bound and a tuning mode have all
been removed — every one of them is now either measured from the house
(`learner.py`, `lag.py`) or fixed at a value with a physical justification.

What is left divides cleanly: an entity to read, an entity to write, or a
genuine occupant preference. The dividing line is the one this project has
always stated — config describes the occupant, never the building.
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

from .const import (
    CONFIG_ENTRY_VERSION,
    CONF_COMFORT_MIN_C,
    CONF_ENABLE_DATA_LOGGING,
    CONF_ENABLE_PRICE_COMPENSATION,
    CONF_ENABLE_SOLAR_INPUT,
    CONF_ENABLE_WIND_INPUT,
    CONF_ENABLE_WEATHER_LOOKAHEAD,
    CONF_HEAT_CURVE_OFFSET_ENTITY,
    CONF_HEAT_CURVE_OFFSET_INVERT,
    CONF_HEATING_TYPE,
    CONF_INDOOR_TARGET_TEMPERATURE,
    CONF_INDOOR_TEMP_SENSOR,
    CONF_NORDPOOL_PRICE_ENTITY,
    CONF_OHMONWIFI_HOST,
    CONF_OUTDOOR_TEMP_SENSOR,
    CONF_OUTPUT_MODE,
    CONF_OUTPUT_NUMBER_ENTITY,
    CONF_PRICE_SIGNIFICANCE_FLOOR,
    CONF_WEATHER_ENTITY,
    DEFAULT_COMFORT_MIN_C,
    DEFAULT_ENABLE_DATA_LOGGING,
    DEFAULT_ENABLE_PRICE_COMPENSATION,
    DEFAULT_ENABLE_SOLAR_INPUT,
    DEFAULT_ENABLE_WIND_INPUT,
    DEFAULT_ENABLE_WEATHER_LOOKAHEAD,
    DEFAULT_HEAT_CURVE_OFFSET_INVERT,
    DEFAULT_INDOOR_TARGET_TEMPERATURE,
    DEFAULT_OUTPUT_MODE,
    DEFAULT_PRICE_SIGNIFICANCE_FLOOR,
    DOMAIN,
    OUTPUT_MODE_HEAT_CURVE_OFFSET,
    OUTPUT_MODES,
)
from .lag import DEFAULT_HEATING_TYPE, HEATING_TYPES

# UI-only grouping for the two outdoor-spoofing targets on the "output_spoof"
# page, so each gets its own header. The entry's stored options stay flat, so
# coordinator.py never learns that sections exist (see `async_step_output_spoof`,
# which re-flattens on submit).
SECTION_OUTPUT_NUMBER = "output_number_push"
SECTION_OHMONWIFI = "ohmonwifi_direct"

# Timeout for validating an OhmOnWifi host at options-save time. This happens
# once, interactively, while the user is watching the form, so it can be a
# little more generous than the per-cycle push timeout.
OHMONWIFI_VALIDATE_TIMEOUT_SECONDS = 5.0


async def _async_ohmonwifi_reachable(hass: HomeAssistant, host: str) -> bool:
    """Best-effort reachability check against the device's own `/info`
    endpoint, so a typo'd address fails where the user can see it rather than
    silently doing nothing every cycle."""
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
    """The whole of setup: two sensors, a temperature, and what the emitters are."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_NAME, default=defaults.get(CONF_NAME, "TrueTemp")
            ): str,
            vol.Required(
                CONF_INDOOR_TEMP_SENSOR, default=defaults.get(CONF_INDOOR_TEMP_SENSOR)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Required(
                CONF_OUTDOOR_TEMP_SENSOR, default=defaults.get(CONF_OUTDOOR_TEMP_SENSOR)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            # Optional, and can also be set later from the options' "sources"
            # page — shown here too because most installs have one or both
            # ready at setup time and would otherwise have to find the
            # options flow just to fill them in.
            #
            # `vol.Any(None, ...)` because HA's frontend submits an explicit
            # null for an untouched/cleared optional entity picker, and a bare
            # EntitySelector only accepts a string.
            vol.Optional(
                CONF_WEATHER_ENTITY, default=defaults.get(CONF_WEATHER_ENTITY)
            ): vol.Any(
                None,
                selector.EntitySelector(selector.EntitySelectorConfig(domain="weather")),
            ),
            vol.Optional(
                CONF_NORDPOOL_PRICE_ENTITY,
                default=defaults.get(CONF_NORDPOOL_PRICE_ENTITY),
            ): vol.Any(
                None,
                selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            ),
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
            # Asked because it is the fallback for how long this house takes to
            # respond, used until `lag.py` has measured the real figures from a
            # few days of operation. After that it stops mattering — which is
            # why it is the only question about the building that survives.
            vol.Required(
                CONF_HEATING_TYPE,
                default=defaults.get(CONF_HEATING_TYPE, DEFAULT_HEATING_TYPE),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(HEATING_TYPES),
                    translation_key="heating_type",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


class TrueTempConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle initial setup of a TrueTemp zone."""

    VERSION = CONFIG_ENTRY_VERSION

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_INDOOR_TEMP_SENSOR])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)
        return self.async_show_form(step_id="user", data_schema=_user_data_schema())

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            if user_input[CONF_INDOOR_TEMP_SENSOR] != entry.unique_id:
                await self.async_set_unique_id(user_input[CONF_INDOOR_TEMP_SENSOR])
                self._abort_if_unique_id_configured()
            return self.async_update_reload_and_abort(
                entry, title=user_input[CONF_NAME], data=user_input
            )
        return self.async_show_form(
            step_id="reconfigure",
            # Merged with options: `weather_entity`/`nordpool_price_entity`
            # may have been set or cleared later from the options "sources"
            # page, which is where the current value actually lives once
            # that's happened (options override data — see `_entry_value` in
            # coordinator.py).
            data_schema=_user_data_schema({**entry.data, **entry.options}),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> TrueTempOptionsFlow:
        return TrueTempOptionsFlow()


class TrueTempOptionsFlow(config_entries.OptionsFlow):
    """Three focused pages, each saving independently.

    Each page's submit handler merges its own keys over the currently stored
    options rather than writing only its own fields, so pages can be edited in
    any order without one page's stale defaults clobbering another's freshly
    saved values.
    """

    # Set by `async_step_output` before it hands off to whichever mode-specific
    # page follows (`async_step_output_spoof` or `async_step_output_curve`),
    # which merge it back in on save. Not persisted mid-flow anywhere else.
    _output_common: dict[str, Any]

    def _current(self) -> dict[str, Any]:
        """Stored options layered over setup data — what each page shows."""
        return {**self.config_entry.data, **self.config_entry.options}

    def _save(self, updates: dict[str, Any]) -> config_entries.ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, **updates})

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["settings", "sources", "price", "output"]
        )

    # --- Page: the building's own sensors and logging -----------------------

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        current = self._current()
        errors: dict[str, str] = {}

        if user_input is not None:
            new_indoor = user_input[CONF_INDOOR_TEMP_SENSOR]
            if new_indoor != self.config_entry.unique_id:
                # OptionsFlow has no async_set_unique_id/_abort_if_unique_id_configured
                # (those belong to ConfigFlow, used by async_step_reconfigure above) —
                # so the same "don't let two zones share an indoor sensor" check is
                # done by hand here.
                duplicate = any(
                    other.entry_id != self.config_entry.entry_id
                    and other.unique_id == new_indoor
                    for other in self.hass.config_entries.async_entries(DOMAIN)
                )
                if duplicate:
                    errors["base"] = "already_configured"
                else:
                    self.hass.config_entries.async_update_entry(
                        self.config_entry, unique_id=new_indoor
                    )

            if not errors:
                return self._save(user_input)
            current = {**current, **user_input}

        return self.async_show_form(
            step_id="settings",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_INDOOR_TEMP_SENSOR,
                        default=current.get(CONF_INDOOR_TEMP_SENSOR),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    ),
                    vol.Required(
                        CONF_OUTDOOR_TEMP_SENSOR,
                        default=current.get(CONF_OUTDOOR_TEMP_SENSOR),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    ),
                    vol.Required(
                        CONF_HEATING_TYPE,
                        default=current.get(CONF_HEATING_TYPE, DEFAULT_HEATING_TYPE),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=list(HEATING_TYPES),
                            translation_key="heating_type",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    # Not an output method itself, but grouped with the other
                    # building-level toggles rather than the "Outgoing sensors"
                    # page, which is about wiring the value out, not this zone.
                    vol.Required(
                        CONF_ENABLE_DATA_LOGGING,
                        default=current.get(
                            CONF_ENABLE_DATA_LOGGING, DEFAULT_ENABLE_DATA_LOGGING
                        ),
                    ): selector.BooleanSelector(),
                }
            ),
        )

    # --- Page: optional sources ---------------------------------------------

    async def async_step_sources(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return self._save(user_input)

        current = self._current()
        return self.async_show_form(
            step_id="sources",
            data_schema=vol.Schema(
                {
                    # Only the wind and solar terms consume this, and both can
                    # be switched off below — so with both off it is not needed
                    # at all and the per-cycle forecast calls are skipped.
                    #
                    # `vol.Any(None, ...)` because HA's frontend submits an
                    # explicit null for an untouched/cleared optional entity
                    # picker, and a bare EntitySelector only accepts a string.
                    vol.Optional(
                        CONF_WEATHER_ENTITY, default=current.get(CONF_WEATHER_ENTITY)
                    ): vol.Any(
                        None,
                        selector.EntitySelector(
                            selector.EntitySelectorConfig(domain="weather")
                        ),
                    ),
                    vol.Optional(
                        CONF_NORDPOOL_PRICE_ENTITY,
                        default=current.get(CONF_NORDPOOL_PRICE_ENTITY),
                    ): vol.Any(
                        None,
                        selector.EntitySelector(
                            selector.EntitySelectorConfig(domain="sensor")
                        ),
                    ),
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
                        CONF_ENABLE_WEATHER_LOOKAHEAD,
                        default=current.get(
                            CONF_ENABLE_WEATHER_LOOKAHEAD,
                            DEFAULT_ENABLE_WEATHER_LOOKAHEAD,
                        ),
                    ): selector.BooleanSelector(),
                }
            ),
        )

    # --- Page: price ---------------------------------------------------------

    def _price_unit(self) -> str | None:
        """Unit label for the price-significance-floor field below, read from
        the configured price entity's OWN `unit_of_measurement` attribute.

        Deliberately NOT `currency`: on the Nordpool integration this project
        targets, `currency` reports the base ISO currency code ("SEK") even
        when the entity's `price_in_cents` option is on and the values it
        actually publishes are in öre — using it here would silently be off by
        100x on exactly the installs most likely to type a small number into
        this field. `unit_of_measurement` gives the right answer either way
        ("SEK/kWh" or "öre/kWh"). Omitted (None, which the selector shows as
        no unit at all) rather than guessed when the entity or the attribute
        is not available — a wrong label is worse than none.
        """
        entity_id = self._current().get(CONF_NORDPOOL_PRICE_ENTITY)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        unit = state.attributes.get("unit_of_measurement")
        return unit if isinstance(unit, str) else None

    async def async_step_price(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return self._save(user_input)

        # Three fields, because the aggressiveness tier and the cold-caution
        # setting are live entities rather than options — they are adjusted
        # often enough that routing them through a config option (which reloads
        # the entry) would be the wrong home.
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
                    # The only comfort bound in the integration, and it applies
                    # to price compensation alone: how cold the house may get
                    # while chasing a cheap hour. It genuinely binds — the High
                    # tier allows a 3 degC sag, so a 21 degC target reaches this
                    # default exactly.
                    vol.Required(
                        CONF_COMFORT_MIN_C,
                        default=current.get(CONF_COMFORT_MIN_C, DEFAULT_COMFORT_MIN_C),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=5, max=25, step=0.5, unit_of_measurement="°C", mode="box"
                        )
                    ),
                    # The one absolute-money knob left: see
                    # CONF_PRICE_SIGNIFICANCE_FLOOR's docstring in const.py. 0
                    # (the default) means "auto" — a floor derived from this
                    # zone's own price history rather than a fixed number,
                    # since no single constant means the same thing across
                    # every currency and sub-unit Nordpool can report in. min
                    # deliberately allows exactly 0 for that reason; max/step
                    # are otherwise unit-agnostic rather than trying to rescale
                    # bounds by whatever unit `_price_unit` happens to read
                    # this cycle.
                    vol.Required(
                        CONF_PRICE_SIGNIFICANCE_FLOOR,
                        default=current.get(
                            CONF_PRICE_SIGNIFICANCE_FLOOR,
                            DEFAULT_PRICE_SIGNIFICANCE_FLOOR,
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=50,
                            step=0.01,
                            unit_of_measurement=self._price_unit(),
                            mode="box",
                        )
                    ),
                }
            ),
        )

    # --- Page: outgoing sensors ----------------------------------------------
    #
    # Split across steps rather than one page with all three target sections,
    # because HA's options flow has no way to hide a field conditionally
    # within a single page — the mode has to be chosen and submitted first so
    # the next page can be built with only the fields that mode reads. This
    # also means the mode-specific fields' text no longer needs to name the
    # mode ("Used when the output method above is...") since only the
    # relevant page is ever shown.

    async def async_step_output(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        current = self._current()

        if user_input is not None:
            self._output_common = dict(user_input)
            if self._output_common[CONF_OUTPUT_MODE] == OUTPUT_MODE_HEAT_CURVE_OFFSET:
                return await self.async_step_output_curve()
            return await self.async_step_output_spoof()

        return self.async_show_form(
            step_id="output",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_OUTPUT_MODE,
                        default=current.get(CONF_OUTPUT_MODE, DEFAULT_OUTPUT_MODE),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=list(OUTPUT_MODES),
                            translation_key="output_mode",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    async def async_step_output_spoof(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        current = self._current()

        if user_input is not None:
            # Flatten the cosmetic sections straight back into plain top-level
            # keys, so the stored options — and coordinator.py, which knows
            # these only as flat keys — never learn sections exist.
            output_number_data = dict(user_input.pop(SECTION_OUTPUT_NUMBER, {}))
            ohmonwifi_data = dict(user_input.pop(SECTION_OHMONWIFI, {}))
            host = (ohmonwifi_data.get(CONF_OHMONWIFI_HOST) or "").strip()
            user_input[CONF_OUTPUT_NUMBER_ENTITY] = output_number_data.get(
                CONF_OUTPUT_NUMBER_ENTITY
            )
            user_input[CONF_OHMONWIFI_HOST] = host or None
            if host and not await _async_ohmonwifi_reachable(self.hass, host):
                errors["base"] = "cannot_connect"
                # Re-show with what was just submitted rather than the stored
                # options, so nothing else the user typed is lost.
                current = {**current, **user_input}
            else:
                return self._save({**self._output_common, **user_input})

        return self.async_show_form(
            step_id="output_spoof",
            errors=errors,
            data_schema=vol.Schema(
                {
                    # Independent, not alternatives — set one, both or neither.
                    vol.Required(SECTION_OUTPUT_NUMBER): section(
                        vol.Schema(
                            {
                                # `vol.Any(None, ...)` — see the OhmOnWifi host
                                # field below, same reason.
                                vol.Optional(
                                    CONF_OUTPUT_NUMBER_ENTITY,
                                    default=current.get(CONF_OUTPUT_NUMBER_ENTITY),
                                ): vol.Any(
                                    None,
                                    selector.EntitySelector(
                                        selector.EntitySelectorConfig(domain="number")
                                    ),
                                ),
                            }
                        ),
                        {"collapsed": False},
                    ),
                    vol.Required(SECTION_OHMONWIFI): section(
                        vol.Schema(
                            {
                                # `vol.Any(None, ...)` because HA's frontend
                                # submits an explicit null when a previously
                                # non-empty optional field is cleared, and a
                                # bare TextSelector only accepts str.
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

    async def async_step_output_curve(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        current = self._current()

        if user_input is not None:
            return self._save({**self._output_common, **user_input})

        return self.async_show_form(
            step_id="output_curve",
            data_schema=vol.Schema(
                {
                    # `vol.Any(None, ...)` — see the OhmOnWifi host field in
                    # `async_step_output_spoof`, same reason.
                    vol.Optional(
                        CONF_HEAT_CURVE_OFFSET_ENTITY,
                        default=current.get(CONF_HEAT_CURVE_OFFSET_ENTITY),
                    ): vol.Any(
                        None,
                        # Both domains expose the same `set_value` service, so
                        # either a "number" helper/platform entity or a legacy
                        # "input_number" helper works as a push target — see
                        # `_async_push_heat_curve_offset` in coordinator.py.
                        selector.EntitySelector(
                            selector.EntitySelectorConfig(
                                domain=["number", "input_number"]
                            )
                        ),
                    ),
                    # A hardware fact about the pump model, asked once — see
                    # CONF_HEAT_CURVE_OFFSET_INVERT's docstring in const.py for
                    # the convention it flips.
                    vol.Required(
                        CONF_HEAT_CURVE_OFFSET_INVERT,
                        default=current.get(
                            CONF_HEAT_CURVE_OFFSET_INVERT,
                            DEFAULT_HEAT_CURVE_OFFSET_INVERT,
                        ),
                    ): selector.BooleanSelector(),
                }
            ),
        )
