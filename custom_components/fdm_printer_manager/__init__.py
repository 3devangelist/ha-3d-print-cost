"""FDM Printer Manager — your 3D printers in Home Assistant, with the energy and cost of every print.

Printers: Klipper (Moonraker, direct) and Bambu Lab (through ha-bambulab).
Optional: a smart plug with a meter + an electricity price for the cost of each print.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_PANEL,
    DOMAIN,
    PANEL_ELEMENT,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL_PATH,
    STATIC_URL,
)
from .coordinator import PrintCostConfigEntry, PrintCostCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.IMAGE, Platform.EVENT, Platform.SWITCH]

# Entities of the removed history and G-code estimate features.
REMOVED_KEYS = ("print_history", "gcode_estimate", "gcode_file", "estimate_gcode", "refresh_gcode_files")


async def async_setup_entry(hass: HomeAssistant, entry: PrintCostConfigEntry) -> bool:
    """Set up one printer (or only the sidebar panel)."""
    await _async_ensure_panel(hass)
    if entry.data.get(CONF_PANEL):
        return True
    registry = er.async_get(hass)
    for key in REMOVED_KEYS:
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
            if entity.unique_id == f"{entry.entry_id}_{key}":
                registry.async_remove(entity.entity_id)
    coordinator = PrintCostCoordinator(hass, entry)
    await coordinator.async_start()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PrintCostConfigEntry) -> bool:
    """Unload: the active job is flushed to storage and resumed on next setup."""
    if entry.data.get(CONF_PANEL):
        unloaded = True
    else:
        unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        if unloaded:
            await entry.runtime_data.async_stop()
    if unloaded:
        _remove_panel_if_unused(hass, entry)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: PrintCostConfigEntry) -> None:
    """Delete stored jobs and the last print image when the integration is removed."""
    if entry.data.get(CONF_PANEL):
        return
    from .storage import JobStore  # noqa: PLC0415 - only needed here

    await JobStore(hass, entry.entry_id).async_remove()


async def _async_update_listener(hass: HomeAssistant, entry: PrintCostConfigEntry) -> None:
    """Options changed: reload (the running print survives through storage)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_ensure_panel(hass: HomeAssistant) -> None:
    """Register the "FDM Printers" sidebar panel once."""
    data = hass.data.setdefault(DOMAIN, {})
    if data.get("panel"):
        return
    if not data.get("static") and hass.http is not None:
        frontend_dir = Path(__file__).parent / "frontend"
        await hass.http.async_register_static_paths([StaticPathConfig(STATIC_URL, str(frontend_dir), False)])
        data["static"] = True
    version = await hass.async_add_executor_job(_manifest_version)
    frontend.async_register_built_in_panel(
        hass,
        component_name="custom",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        frontend_url_path=PANEL_URL_PATH,
        config={
            "_panel_custom": {
                "name": PANEL_ELEMENT,
                "module_url": f"{STATIC_URL}/{PANEL_ELEMENT}.js?v={version}",
                "embed_iframe": False,
                "trust_external": False,
            }
        },
        require_admin=True,
        update=True,
    )
    data["panel"] = True


def _remove_panel_if_unused(hass: HomeAssistant, unloading: PrintCostConfigEntry) -> None:
    """Take the panel out of the sidebar when no entry of this integration is loaded any more."""
    others = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != unloading.entry_id and e.state is ConfigEntryState.LOADED
    ]
    data = hass.data.get(DOMAIN, {})
    if not others and data.get("panel"):
        frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
        data["panel"] = False


def _manifest_version() -> str:
    """Version string used to bust the browser cache of the panel script."""
    import json  # noqa: PLC0415

    manifest = json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))
    return str(manifest.get("version", "0"))
