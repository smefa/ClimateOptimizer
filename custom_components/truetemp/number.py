"""Number platform: the holiday setback target temperature.

A live entity over one in-memory coordinator field, same reasoning as
select.py/date.py. `native_min_value` tracks the live `comfort_min_c` config
value (`coordinator.comfort_min_c`) so the occupant cannot even select a
holiday target below their own configured comfort floor — `comfort_min_c`
stays the single absolute floor everywhere in this integration, see
holiday.py's module docstring.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import UnitOfTemperature
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
    async_add_entities([HolidayTargetTemperatureNumber(entry.runtime_data, entry)])


class HolidayTargetTemperatureNumber(TrueTempEntity, NumberEntity, RestoreEntity):
    """The indoor target to sag to while the holiday setback holds."""

    _attr_translation_key = "holiday_target_temperature"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_max_value = 30.0
    _attr_native_step = 0.5
    _attr_mode = NumberMode.BOX

    def __init__(
        self, coordinator: TrueTempCoordinator, entry: TrueTempConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_holiday_target_temperature"

    @property
    def available(self) -> bool:
        """Always settable: a local value, not fetched data. Same override
        the other live-control-surface entities carry."""
        return True

    @property
    def native_min_value(self) -> float:
        return self.coordinator.comfort_min_c

    @property
    def native_value(self) -> float:
        return self.coordinator.holiday_target_c

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None:
            return
        try:
            self.coordinator.holiday_target_c = float(last_state.state)
        except (TypeError, ValueError):
            pass

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.holiday_target_c = value
        self.async_write_ha_state()
        # Reflect the new target promptly rather than waiting for the next
        # polled cycle.
        await self.coordinator.async_request_refresh()
