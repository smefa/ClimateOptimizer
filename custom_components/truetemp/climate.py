"""Climate platform for TrueTemp: the zone thermostat.

This is the integration's single user-facing control surface. It replaces what
used to be `number.<name>_indoor_target_temperature`: a bare number could not
be driven by the thermostat card, by voice assistants (Google/Alexa/HomeKit
have no sensible mapping for "set this number to 21"), or by any of the
climate-domain scheduler integrations. The value it edits is the same one —
`coordinator.indoor_target_c` — so nothing about the control law changed.

The entity is a thin view over three in-memory coordinator fields rather than
over config entry options, for the reason spelled out in coordinator.py: a
config option change reloads the entry, and these three get nudged often (by
hand, by schedules, by automations), so they must not be options. State
survives restarts via RestoreEntity, the same pattern the activation switch and
the selects use.

    target_temperature  ->  coordinator.indoor_target_c
    hvac_mode           ->  coordinator.is_active
    preset_mode         ->  coordinator.price_comfort_tier

`switch.<name>_compensation_active` and `select.<name>_price_comfort_tier` are
views over those same two latter fields and still work; they are not separate
state, so the two surfaces cannot disagree.
"""

from __future__ import annotations

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import TrueTempConfigEntry
from .coordinator import TrueTempCoordinator
from .heuristic import PRICE_TIER_MID, PRICE_TIERS, HeuristicResult
from .sensor import TrueTempEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrueTempConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([TrueTempThermostat(entry.runtime_data, entry)])


class TrueTempThermostat(TrueTempEntity, ClimateEntity, RestoreEntity):
    """The zone thermostat: comfort target, on/off, and price aggressiveness.

    On `hvac_mode`, which is the one mapping here that is not literal. OFF does
    NOT mean the house stops being heated — it means compensation stops being
    applied: the published output falls back to the raw outdoor temperature and
    the heat pump carries on under its own heat curve. The controller keeps
    computing and its recommendation stays readable on the main sensor's
    `recommended_compensated_outdoor_temp_c` attribute, so OFF is also how you
    preview it. This is the same flag as
    `switch.<name>_compensation_active`, under a name that says so more plainly.

    Note that OFF also pauses learning, and necessarily so: an integral
    controller cannot learn from an output that is not applied, because it
    never observes the response to its own action.

    AUTO rather than HEAT for the on state, because the controller chooses the
    setpoint continuously from price, weather and what it has learned — there
    is no mode in which the user's number is applied as-is.
    """

    _attr_translation_key = "thermostat"
    # The device's primary entity, so it takes the device (zone) name and gets
    # a clean `climate.<zone>` entity_id. The translation key above is still
    # needed for the preset names.
    _attr_name = None

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.AUTO, HVACMode.OFF]
    _attr_preset_modes = list(PRICE_TIERS)
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    # Same bounds and step the number entity carried.
    _attr_min_temp = 10.0
    _attr_max_temp = 30.0
    _attr_target_temperature_step = 0.5

    def __init__(
        self, coordinator: TrueTempCoordinator, entry: TrueTempConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_thermostat"

    @property
    def available(self) -> bool:
        """Always controllable: every field here is a local value, not fetched
        data.

        Overrides CoordinatorEntity's default (tied to last_update_success) so
        the target can still be adjusted while a required source is down and the
        sensors are showing unavailable — the new value simply takes effect on
        the next successful cycle. Same override the number, switch and selects
        carry.
        """
        return True

    @property
    def current_temperature(self) -> float | None:
        result: HeuristicResult | None = self.coordinator.data
        return result.indoor_temp_c if result else None

    @property
    def target_temperature(self) -> float:
        return self.coordinator.indoor_target_c

    @property
    def hvac_mode(self) -> HVACMode:
        return HVACMode.AUTO if self.coordinator.is_active else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction | None:
        """Whether compensation is currently reaching the output.

        Reports the optimizer's own state, not the heat pump's — this
        integration publishes a compensated outdoor temperature and never learns
        whether the compressor actually ran. IDLE is the hard heating limit:
        active, but forced to publish the warm ceiling regardless.
        """
        if not self.coordinator.is_active:
            return HVACAction.OFF
        result: HeuristicResult | None = self.coordinator.data
        if result is None:
            return None
        return HVACAction.IDLE if result.heating_hard_limit_engaged else HVACAction.HEATING

    @property
    def preset_mode(self) -> str:
        return self.coordinator.price_comfort_tier

    @property
    def extra_state_attributes(self) -> dict:
        """The gap between what was asked for and what price compensation is
        actually holding to, so the thermostat's more-info dialog explains
        itself without a trip to the diagnostic sensors."""
        result: HeuristicResult | None = self.coordinator.data
        if result is None:
            return {}
        return {
            "effective_target_temperature": result.effective_indoor_target_c,
            "price_shift_applied_c": result.price_shift_applied_c,
            "learned_offset_c": result.learned_offset_c,
            "reason": result.reason,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None:
            # Cold start, including the first run after upgrading from the
            # number entity: the unique_id changed, so there is nothing to
            # restore and the coordinator's own defaults stand (target seeded
            # from the config entry, compensation off).
            return
        try:
            self.coordinator.indoor_target_c = float(
                last_state.attributes[ATTR_TEMPERATURE]
            )
        except (KeyError, TypeError, ValueError):
            pass
        if last_state.state in (HVACMode.AUTO, HVACMode.OFF):
            self.coordinator.is_active = last_state.state == HVACMode.AUTO
        preset = last_state.attributes.get("preset_mode")
        if preset in PRICE_TIERS:
            self.coordinator.price_comfort_tier = preset
        # The coordinator's first refresh ran before this entity existed to
        # restore anything, on default-derived values that may already have
        # been pushed to hardware. Request a fresh cycle now rather than
        # waiting up to a full update interval for the restored values to
        # reach the output.
        await self.coordinator.async_request_refresh()

    async def async_set_temperature(self, **kwargs) -> None:
        if (value := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        self.coordinator.indoor_target_c = float(value)
        self.async_write_ha_state()
        # Reflect the new target promptly rather than waiting for the next
        # polled cycle.
        await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self.coordinator.is_active = hvac_mode == HVACMode.AUTO
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.AUTO)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        # Anything unrecognized lands on Mid rather than raising, matching the
        # price-tier select's behaviour.
        if preset_mode not in PRICE_TIERS:
            preset_mode = PRICE_TIER_MID
        self.coordinator.price_comfort_tier = preset_mode
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
