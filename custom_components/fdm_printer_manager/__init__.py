"""FDM Printer Manager — your 3D printers in Home Assistant, with the energy and cost of every print.

Printers: Klipper (Moonraker, direct) and Bambu Lab (through ha-bambulab).
Optional: a smart plug with a meter + price, HaspelSync and Spoolman for filament.
"""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .coordinator import PrintCostConfigEntry, PrintCostCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.IMAGE, Platform.EVENT, Platform.SWITCH]

# Entities of the removed history and G-code estimate features.
REMOVED_KEYS = ("print_history", "gcode_estimate", "gcode_file", "estimate_gcode", "refresh_gcode_files")


async def async_setup_entry(hass: HomeAssistant, entry: PrintCostConfigEntry) -> bool:
    """Set up one printer."""
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
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: PrintCostConfigEntry) -> None:
    """Delete stored jobs and the last print image when the integration is removed."""
    from .storage import JobStore  # noqa: PLC0415 - only needed here

    await JobStore(hass, entry.entry_id).async_remove()


async def _async_update_listener(hass: HomeAssistant, entry: PrintCostConfigEntry) -> None:
    """Options changed: reload (the running print survives through storage)."""
    await hass.config_entries.async_reload(entry.entry_id)
