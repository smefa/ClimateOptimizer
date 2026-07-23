"""Config flow for ClimateOptimizer.

Initial setup only asks for entity selections + the indoor target temperature.
Every tunable coefficient lives in the options flow so it can be changed later
without deleting and re-adding the integration.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_COMFORT_MAX_C,
    CONF_COMFORT_MIN_C,
    CONF_ENABLE_PRICE_COMPENSATION,
    CONF_INDOOR_TARGET_TEMPERATURE,
    CONF_INDOOR_TEMP_SENSOR,
    CONF_K_INDOOR,
    CONF_K_SUN,
    CONF_K_WIND,
    CONF_NORDPOOL_PRICE_ENTITY,
    CONF_OUTDOOR_TEMP_SENSOR,
    CONF_PRICE_MAX_DROP_C,
    CONF_PRICE_THRESHOLD_MAX,
    CONF_PRICE_THRESHOLD_START,
    CONF_UPDATE_INTERVAL_MINUTES,
    CONF_WEATHER_ENTITY,
    DEFAULT_COMFORT_MAX_C,
    DEFAULT_COMFORT_MIN_C,
    DEFAULT_ENABLE_PRICE_COMPENSATION,
    DEFAULT_INDOOR_TARGET_TEMPERATURE,
    DEFAULT_K_INDOOR,
    DEFAULT_K_SUN,
    DEFAULT_K_WIND,
    DEFAULT_PRICE_MAX_DROP_C,
    DEFAULT_PRICE_THRESHOLD_MAX,
    DEFAULT_PRICE_THRESHOLD_START,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
)


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
            vol.Required(
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
    """All tunable coefficients and toggles, editable without reinstalling."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = {**self.config_entry.data, **self.config_entry.options}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_INDOOR_TARGET_TEMPERATURE,
                    default=current.get(
                        CONF_INDOOR_TARGET_TEMPERATURE, DEFAULT_INDOOR_TARGET_TEMPERATURE
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=10, max=30, step=0.5, unit_of_measurement="°C", mode="box"
                    )
                ),
                vol.Required(
                    CONF_WEATHER_ENTITY,
                    default=current.get(CONF_WEATHER_ENTITY),
                ): selector.EntitySelector(selector.EntitySelectorConfig(domain="weather")),
                vol.Optional(
                    CONF_NORDPOOL_PRICE_ENTITY,
                    default=current.get(CONF_NORDPOOL_PRICE_ENTITY),
                ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
                vol.Required(
                    CONF_ENABLE_PRICE_COMPENSATION,
                    default=current.get(
                        CONF_ENABLE_PRICE_COMPENSATION, DEFAULT_ENABLE_PRICE_COMPENSATION
                    ),
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_K_INDOOR, default=current.get(CONF_K_INDOOR, DEFAULT_K_INDOOR)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=10, step=0.1, mode="box")
                ),
                vol.Required(
                    CONF_K_WIND, default=current.get(CONF_K_WIND, DEFAULT_K_WIND)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=3, step=0.05, mode="box")
                ),
                vol.Required(
                    CONF_K_SUN, default=current.get(CONF_K_SUN, DEFAULT_K_SUN)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=10, step=0.1, mode="box")
                ),
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
                    CONF_PRICE_THRESHOLD_START,
                    default=current.get(
                        CONF_PRICE_THRESHOLD_START, DEFAULT_PRICE_THRESHOLD_START
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=20, step=0.1, mode="box")
                ),
                vol.Required(
                    CONF_PRICE_THRESHOLD_MAX,
                    default=current.get(
                        CONF_PRICE_THRESHOLD_MAX, DEFAULT_PRICE_THRESHOLD_MAX
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=20, step=0.1, mode="box")
                ),
                vol.Required(
                    CONF_PRICE_MAX_DROP_C,
                    default=current.get(CONF_PRICE_MAX_DROP_C, DEFAULT_PRICE_MAX_DROP_C),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=10, step=0.1, unit_of_measurement="°C", mode="box"
                    )
                ),
                vol.Required(
                    CONF_UPDATE_INTERVAL_MINUTES,
                    default=current.get(
                        CONF_UPDATE_INTERVAL_MINUTES, DEFAULT_UPDATE_INTERVAL_MINUTES
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5, max=60, step=1, unit_of_measurement="min", mode="box"
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
