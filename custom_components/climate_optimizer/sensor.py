"""Sensor platform for ClimateOptimizer."""

from __future__ import annotations

from dataclasses import asdict

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ClimateOptimizerConfigEntry
from .const import DOMAIN
from .coordinator import ClimateOptimizerCoordinator
from .heuristic import HeuristicResult


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClimateOptimizerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            CompensatedOutdoorTempSensor(coordinator, entry),
            IndoorTemperatureErrorSensor(coordinator, entry),
            PriceShiftAppliedSensor(coordinator, entry),
        ]
    )


class ClimateOptimizerEntity(CoordinatorEntity[ClimateOptimizerCoordinator]):
    """Common device grouping for all ClimateOptimizer entities of one entry."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="ClimateOptimizer",
            model="Heuristic v1",
            entry_type=DeviceEntryType.SERVICE,
        )


class CompensatedOutdoorTempSensor(ClimateOptimizerEntity, SensorEntity):
    """The main output: the compensated outdoor temperature, fully explained."""

    _attr_translation_key = "compensated_outdoor_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_compensated_outdoor_temperature"

    @property
    def native_value(self) -> float | None:
        result: HeuristicResult | None = self.coordinator.data
        return result.compensated_outdoor_temp_c if result else None

    @property
    def extra_state_attributes(self) -> dict:
        result: HeuristicResult | None = self.coordinator.data
        if result is None:
            return {}
        attrs = asdict(result)
        attrs.pop("compensated_outdoor_temp_c", None)
        return attrs


class IndoorTemperatureErrorSensor(ClimateOptimizerEntity, SensorEntity):
    """Diagnostic: target minus actual indoor temperature."""

    _attr_translation_key = "indoor_temperature_error"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_indoor_temperature_error"

    @property
    def native_value(self) -> float | None:
        result: HeuristicResult | None = self.coordinator.data
        if result is None or result.indoor_temp_c is None:
            return None
        return round(result.indoor_target_c - result.indoor_temp_c, 2)


class PriceShiftAppliedSensor(ClimateOptimizerEntity, SensorEntity):
    """Diagnostic: how much the comfort target is currently being lowered for price."""

    _attr_translation_key = "price_shift_applied"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_price_shift_applied"

    @property
    def native_value(self) -> float | None:
        result: HeuristicResult | None = self.coordinator.data
        return result.price_shift_applied_c if result else None
