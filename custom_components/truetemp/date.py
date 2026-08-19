"""Date platform: the two holiday-window endpoints.

Live entities over in-memory coordinator fields, same reasoning as
select.py's `_LiveSelect` — an options change reloads the config entry, and
these get set once per trip (by hand, or by an automation), so they must not
be config options. Each restores its own value across restarts via
RestoreEntity. Date entities only, not date+time: the plan's "Dates" note —
the return deadline uses one fixed time-of-day (`holiday.HOLIDAY_RETURN_TIME`),
not something the occupant picks, to keep the card simple.
"""

from __future__ import annotations

from datetime import date

from homeassistant.components.date import DateEntity
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
            HolidayStartDate(entry.runtime_data, entry),
            HolidayEndDate(entry.runtime_data, entry),
        ]
    )


class _LiveDate(TrueTempEntity, DateEntity, RestoreEntity):
    """A date entity over one in-memory coordinator field."""

    _field: str

    @property
    def available(self) -> bool:
        """Always settable: a local value, not fetched data. Same override
        the other live-control-surface entities carry."""
        return True

    @property
    def native_value(self) -> date | None:
        return getattr(self.coordinator, self._field)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (None, "unknown", "unavailable"):
            return
        try:
            setattr(self.coordinator, self._field, date.fromisoformat(last_state.state))
        except ValueError:
            pass

    async def async_set_value(self, value: date) -> None:
        setattr(self.coordinator, self._field, value)
        self.async_write_ha_state()
        # Reflect the new date promptly rather than waiting for the next
        # polled cycle.
        await self.coordinator.async_request_refresh()


class HolidayStartDate(_LiveDate):
    """The day the holiday setback begins (midnight local time)."""

    _attr_translation_key = "holiday_start"
    _field = "holiday_start"

    def __init__(
        self, coordinator: TrueTempCoordinator, entry: TrueTempConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_holiday_start"


class HolidayEndDate(_LiveDate):
    """The day the house must be back at the normal target by, at
    `holiday.HOLIDAY_RETURN_TIME`."""

    _attr_translation_key = "holiday_end"
    _field = "holiday_end"

    def __init__(
        self, coordinator: TrueTempCoordinator, entry: TrueTempConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_holiday_end"
