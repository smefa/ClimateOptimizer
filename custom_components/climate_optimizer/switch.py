"""Switch platform for ClimateOptimizer: the learn-mode/live activation toggle."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
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
    async_add_entities([ActiveSwitch(entry.runtime_data, entry)])


class ActiveSwitch(ClimateOptimizerEntity, SwitchEntity, RestoreEntity):
    """Master on/off for applying compensation.

    Off ("learn mode"): the compensated-temperature sensor publishes the raw
    outdoor temperature unmodified, and the RC shadow model treats this cycle
    as zero applied compensation delta (see coordinator._update_rc_shadow_model).
    The heuristic itself keeps running either way — its recommendation is
    always available via the sensor's `recommended_compensated_outdoor_temp_c`
    attribute so you can preview it before switching this on.

    Defaults to off on a brand-new install (and, since this entity didn't
    exist before, on the first restart after upgrading an existing install)
    so compensation only ever goes live on an explicit opt-in. State is
    restored across restarts thereafter via RestoreEntity.
    """

    _attr_translation_key = "active"

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_active"

    @property
    def is_on(self) -> bool:
        return self.coordinator.is_active

    @property
    def available(self) -> bool:
        """Always controllable: this is a local flag, not fetched data.

        Overrides CoordinatorEntity's default (tied to last_update_success)
        so you can still flip learn mode on/off even while a required source
        is unavailable and the sensors are showing as unavailable.
        """
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self.coordinator.is_active = last_state.state == "on"
        # else: keep the coordinator's cold-start default (off / learn mode).

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.is_active = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.is_active = False
        self.async_write_ha_state()
