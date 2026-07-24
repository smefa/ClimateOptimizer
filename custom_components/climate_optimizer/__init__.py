"""The ClimateOptimizer integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event

from .coordinator import ClimateOptimizerCoordinator

PLATFORMS = [Platform.NUMBER, Platform.SENSOR, Platform.SWITCH]

type ClimateOptimizerConfigEntry = ConfigEntry[ClimateOptimizerCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: ClimateOptimizerConfigEntry) -> bool:
    coordinator = ClimateOptimizerCoordinator(hass, entry)
    # Restore the RC shadow model's persisted estimator state (if any) BEFORE
    # the first refresh, so the first cycle already continues from prior
    # learning instead of the cold-start prior. Strictly additive: this never
    # raises and cleanly cold-starts on empty/corrupt/incompatible state.
    await coordinator.async_load_rc_state()
    # Deliberately async_refresh(), not async_config_entry_first_refresh():
    # the latter turns a failed first cycle (e.g. the outdoor sensor still
    # being unavailable while HA is starting up) into ConfigEntryNotReady,
    # which aborts setup before any entities are even created — the
    # integration then looks like it failed to load at all. async_refresh()
    # never raises; entities still get created below and report themselves
    # unavailable via CoordinatorEntity.available until a cycle succeeds,
    # which the coordinator keeps retrying on its normal schedule.
    await coordinator.async_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Don't rely solely on the coordinator's own polling interval to notice
    # a required/soft-degraded source recovering: react immediately to that
    # source's own state changes too. See
    # ClimateOptimizerCoordinator.watched_entity_ids for why.
    watched = coordinator.watched_entity_ids()
    if watched:

        async def _async_source_state_changed(
            event: Event[EventStateChangedData],
        ) -> None:
            await coordinator.async_request_refresh()

        entry.async_on_unload(
            async_track_state_change_event(hass, watched, _async_source_state_changed)
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ClimateOptimizerConfigEntry) -> bool:
    # Flush any pending debounced RC-state write before teardown: HA only
    # auto-flushes Store.async_delay_save on full shutdown, not on an entry
    # reload, so without this the most recent learning could be lost on every
    # options change / reload. Strictly additive and self-guarded.
    await entry.runtime_data.async_save_rc_state_now()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: ClimateOptimizerConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
