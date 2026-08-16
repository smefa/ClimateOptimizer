"""The TrueTemp integration."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    FRONTEND_CARD_URL,
    FRONTEND_JS_VERSION,
    FRONTEND_STATIC_URL_PREFIX,
    KNOWN_CONFIG_KEYS,
)
from .coordinator import TrueTempCoordinator

PLATFORMS = [Platform.CLIMATE, Platform.SELECT, Platform.SENSOR, Platform.SWITCH]

type TrueTempConfigEntry = ConfigEntry[TrueTempCoordinator]

_LOGGER = logging.getLogger(__name__)

_FRONTEND_REGISTERED = f"{DOMAIN}_frontend_registered"


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Drop stored settings that no version of this code reads any more.

    v0.2.0 deleted the RC estimator, the MPC planner, the hand-typed control
    gains and the drawn cold taper, but deleting the code that reads a key does
    not delete the key: the options flow saves
    `{**existing_options, **page_fields}`, so anything ever written is carried
    forward by every later edit. An entry created before v0.2.0 still holds
    around nineteen of those, which show up in diagnostics and in the raw entry
    and read as live configuration when they are inert.

    Pruning is by allow-list (`KNOWN_CONFIG_KEYS`) rather than by naming the
    dead keys, so this is correct for an entry from any older version without
    needing a table per version. Nothing is added: a key that is simply absent
    keeps falling back to its default, which is already how the code treats it.
    """
    if entry.version > CONFIG_ENTRY_VERSION:
        # Written by a newer version than this code understands. Refusing is
        # the only safe move — HA will show the entry as needing a newer build
        # rather than us silently pruning keys we don't know about yet.
        return False

    stray = sorted((set(entry.data) | set(entry.options)) - KNOWN_CONFIG_KEYS)
    if stray:
        _LOGGER.info(
            "Removing %d superseded setting(s) from %s: %s",
            len(stray),
            entry.title,
            ", ".join(stray),
        )
    hass.config_entries.async_update_entry(
        entry,
        data={k: v for k, v in entry.data.items() if k in KNOWN_CONFIG_KEYS},
        options={k: v for k, v in entry.options.items() if k in KNOWN_CONFIG_KEYS},
        version=CONFIG_ENTRY_VERSION,
    )
    return True


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the bundled demo Lovelace card and auto-add it as a resource.

    The whole www/ directory is registered, not just truetemp-card.js: the
    card `import`s its per-language strings from www/lang/*.js, and those
    files need to be reachable at their own URLs for that to resolve.

    Guarded by a hass.data flag rather than being tied to one config entry:
    a second TrueTemp zone would otherwise try to register the same
    static path twice, which raises.
    """
    if hass.data.get(_FRONTEND_REGISTERED):
        return
    hass.data[_FRONTEND_REGISTERED] = True
    www_path = Path(__file__).parent / "www"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(FRONTEND_STATIC_URL_PREFIX, str(www_path), cache_headers=True)]
    )
    add_extra_js_url(hass, f"{FRONTEND_CARD_URL}?v={FRONTEND_JS_VERSION}")


async def async_setup_entry(hass: HomeAssistant, entry: TrueTempConfigEntry) -> bool:
    await _async_register_frontend(hass)
    coordinator = TrueTempCoordinator(hass, entry)
    # Restore the learned offset table and lag buffer (if any) BEFORE the first
    # refresh, so the first cycle continues from prior learning rather than
    # from a cold start. Strictly additive: this never raises and cleanly cold
    # starts on empty, corrupt or version-mismatched state.
    await coordinator.async_load_state()
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
    # TrueTempCoordinator.watched_entity_ids for why.
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


async def async_unload_entry(hass: HomeAssistant, entry: TrueTempConfigEntry) -> bool:
    # Flush any pending debounced write before teardown: HA only auto-flushes
    # Store.async_delay_save on full shutdown, not on an entry reload, so
    # without this the most recent learning is lost on every options change.
    await entry.runtime_data.async_save_state_now()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    # Otherwise a Repair raised while a source was down outlives the
    # integration that raised it (or the reload that briefly tears it down).
    entry.runtime_data.clear_source_issues()
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: TrueTempConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
