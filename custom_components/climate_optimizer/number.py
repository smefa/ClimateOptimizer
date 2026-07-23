"""Number platform for ClimateOptimizer: the live indoor target temperature."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import ClimateOptimizerConfigEntry
from .coordinator import ClimateOptimizerCoordinator
from .sensor import ClimateOptimizerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClimateOptimizerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([IndoorTargetTemperatureNumber(entry.runtime_data, entry)])


class IndoorTargetTemperatureNumber(ClimateOptimizerEntity, NumberEntity, RestoreEntity):
    """The indoor target temperature, adjustable live from a dashboard/automation.

    Backed by an in-memory coordinator value rather than a config entry
    option — see coordinator.py's `indoor_target_c` for why (changing a
    config option triggers a full entry reload, which would wipe the RC
    estimator's learning progress on every nudge of a value people expect to
    adjust often). State is restored across restarts via RestoreEntity, same
    pattern as the activation switch.
    """

    _attr_translation_key = "indoor_target_temperature"
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = 10.0
    _attr_native_max_value = 30.0
    _attr_native_step = 0.5

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_indoor_target_temperature"

    @property
    def native_value(self) -> float:
        return self.coordinator.indoor_target_c

    @property
    def available(self) -> bool:
        """Always settable: this is a local value, not fetched data.

        Overrides CoordinatorEntity's default (tied to last_update_success)
        so you can still adjust the target while a required source is
        unavailable and the sensors are showing as unavailable — the new
        value just takes effect on the next successful cycle.
        """
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            try:
                self.coordinator.indoor_target_c = float(last_state.state)
            except (TypeError, ValueError):
                pass
        # else: keep the coordinator's cold-start default (seeded from the
        # config entry's initial setup value).

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.indoor_target_c = value
        self.async_write_ha_state()
        # Reflect the new target promptly rather than waiting for the next
        # polled cycle.
        await self.coordinator.async_request_refresh()
