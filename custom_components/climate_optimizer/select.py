"""Select platform for ClimateOptimizer: the live price comfort tier and the
manual/auto tuning-mode switch."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import ClimateOptimizerConfigEntry
from .autotune import TUNING_MODE_MANUAL, TUNING_MODES
from .coordinator import ClimateOptimizerCoordinator
from .heuristic import PRICE_TIER_MID, PRICE_TIERS
from .sensor import ClimateOptimizerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClimateOptimizerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities(
        [
            PriceComfortTierSelect(entry.runtime_data, entry),
            TuningModeSelect(entry.runtime_data, entry),
        ]
    )


class PriceComfortTierSelect(ClimateOptimizerEntity, SelectEntity, RestoreEntity):
    """How aggressively price compensation trades comfort for cheaper energy.

    Low barely reacts (max comfort); High brakes hard on spikes (max saving).
    The tier bundles the sag limit, response convexity, pre-brake lead time and
    pre-charge flag — see heuristic.PRICE_TIERS.

    Backed by an in-memory coordinator value rather than a config option — same
    reasoning as the indoor target number and activation switch: it's flipped
    often (and by automations), and a config option would reload the entry and
    wipe RC learning on every change. State is restored across restarts via
    RestoreEntity.
    """

    _attr_translation_key = "price_comfort_tier"
    _attr_options = list(PRICE_TIERS)

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_price_comfort_tier"

    @property
    def current_option(self) -> str:
        return self.coordinator.price_comfort_tier

    @property
    def available(self) -> bool:
        """Always settable: this is a local value, not fetched data (same
        override as the target-temperature number and activation switch)."""
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in PRICE_TIERS:
            self.coordinator.price_comfort_tier = last_state.state
        # else: keep the coordinator's cold-start default (seeded from config).

    async def async_select_option(self, option: str) -> None:
        if option not in PRICE_TIERS:
            option = PRICE_TIER_MID
        self.coordinator.price_comfort_tier = option
        self.async_write_ha_state()
        # Reflect the new tier promptly rather than waiting for the next cycle.
        await self.coordinator.async_request_refresh()


class TuningModeSelect(ClimateOptimizerEntity, SelectEntity, RestoreEntity):
    """Whether the controller runs on hand-configured coefficients or on ones
    derived from the learned RC model (see autotune.py).

    Manual reads k_indoor/k_wind/k_sun/k_price and the three cold-taper values
    straight from the options; Auto replaces all of them with values derived
    from the house's own estimated thermal parameters. The derivation runs and
    is published on the auto-tune diagnostic sensor in BOTH modes, so this
    switch can be flipped after watching derived-vs-manual rather than on faith.

    Live rather than a config option for the same reason as the price tier: an
    options change reloads the entry, and A/B comparison is the entire point of
    having the switch. State is restored across restarts via RestoreEntity, and
    seeded from CONF_TUNING_MODE on the very first run.
    """

    _attr_translation_key = "tuning_mode"
    _attr_options = list(TUNING_MODES)

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_tuning_mode"

    @property
    def current_option(self) -> str:
        return self.coordinator.tuning_mode

    @property
    def available(self) -> bool:
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in TUNING_MODES:
            self.coordinator.tuning_mode = last_state.state

    async def async_select_option(self, option: str) -> None:
        # Anything unrecognized falls back to Manual, never Auto: an unknown
        # value must not silently hand control to the derived coefficients.
        if option not in TUNING_MODES:
            option = TUNING_MODE_MANUAL
        self.coordinator.tuning_mode = option
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
