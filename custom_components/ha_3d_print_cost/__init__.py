"""3D Print Cost — electricity + filament cost of every print.

Aggregates ha-bambulab (printer state), HaspelSync (real consumption),
Spoolman (spool prices, read-only) and a smart plug + price sensor.
"""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import PrintCostConfigEntry, PrintCostCoordinator
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.EVENT, Platform.SELECT, Platform.BUTTON]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the service actions (available even before an entry loads)."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PrintCostConfigEntry) -> bool:
    """Set up one printer."""
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
    """Delete stored jobs when the integration is removed."""
    from .storage import JobStore  # noqa: PLC0415 - only needed here

    await JobStore(hass, entry.entry_id).async_remove()


async def _async_update_listener(hass: HomeAssistant, entry: PrintCostConfigEntry) -> None:
    """Options changed: reload (the running print survives through storage)."""
    await hass.config_entries.async_reload(entry.entry_id)
