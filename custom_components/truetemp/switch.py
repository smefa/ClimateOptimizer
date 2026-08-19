"""Switch platform: the master compensation on/off."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import TrueTempConfigEntry
from .coordinator import TrueTempCoordinator
from .sensor import TrueTempEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrueTempConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities(
        [
            ActiveSwitch(entry.runtime_data, entry),
            HolidayModeSwitch(entry.runtime_data, entry),
        ]
    )


class ActiveSwitch(TrueTempEntity, SwitchEntity, RestoreEntity):
    """Master on/off for applying compensation.

    Off: the sensor publishes the raw outdoor temperature unmodified and the
    heat pump runs on its own curve, exactly as it did before this integration
    was installed. The controller keeps computing, and its recommendation stays
    readable on the main sensor's `recommended_compensated_outdoor_temp_c`
    attribute, so off is how you preview it.

    Defaults off: a fresh install computes and displays its recommendation
    without touching the heat pump until the occupant switches it on.

    The same flag is `hvac_mode` on the climate entity (AUTO/OFF). Both are
    views over `coordinator.is_active` rather than separate state, so they
    cannot disagree.
    """

    _attr_translation_key = "active"

    def __init__(
        self,
        coordinator: TrueTempCoordinator,
        entry: TrueTempConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_active"

    @property
    def is_on(self) -> bool:
        return self.coordinator.is_active

    @property
    def available(self) -> bool:
        """Always controllable: a local flag, not fetched data, so compensation
        can still be switched off while a source is unavailable."""
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in ("on", "off"):
            self.coordinator.is_active = last_state.state == "on"
        # else: keep the coordinator's default (off).

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.is_active = True
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.is_active = False
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()


class HolidayModeSwitch(TrueTempEntity, SwitchEntity, RestoreEntity):
    """Arms/disarms the holiday setback.

    Explicit arming rather than "the two dates alone are active": lets a trip
    be planned ahead (dates set) and then cancelled without clearing them, and
    lets the coordinator's own one-shot auto-disarm (once
    `holiday_result.phase == "done"`, see coordinator.py) fire without
    silently re-arming the next time both dates happen to be set again.
    """

    _attr_translation_key = "holiday_mode"

    def __init__(
        self,
        coordinator: TrueTempCoordinator,
        entry: TrueTempConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_holiday_mode"

    @property
    def is_on(self) -> bool:
        return self.coordinator.holiday_armed

    @property
    def available(self) -> bool:
        """Always controllable: a local flag, not fetched data."""
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in ("on", "off"):
            self.coordinator.holiday_armed = last_state.state == "on"
        # else: keep the coordinator's default (unarmed).

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.holiday_armed = True
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.holiday_armed = False
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
