"""Diagnostics (Settings → Devices → … → Download diagnostics)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import CONF_API_KEY, DOMAIN
from .coordinator import PrintCostConfigEntry

TO_REDACT = {CONF_API_KEY}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: PrintCostConfigEntry) -> dict[str, Any]:
    """Return redacted diagnostics."""
    coordinator = entry.runtime_data
    integration = await async_get_integration(hass, DOMAIN)

    def unit(entity_id: str | None) -> str | None:
        state = hass.states.get(entity_id) if entity_id else None
        return state.attributes.get("unit_of_measurement") if state else None

    sources = coordinator.reader.sources
    active = coordinator.tracker.active_job
    return {
        "version": str(integration.version),
        "config": async_redact_data({**entry.data, **entry.options}, TO_REDACT),
        "currency": coordinator.currency,
        "units": {
            "power": unit(sources.power_entity),
            "energy": unit(sources.energy_entity),
            "price": unit(sources.price_entity),
        },
        "printer": {
            "type": str(coordinator.printer_type),
            "reachable": coordinator.klipper.available if coordinator.klipper else None,
            "last_reading": str(coordinator.klipper.reading) if coordinator.klipper else None,
        },
        "active_job": active.as_dict() if active else None,
        "finalizing_jobs": [j.as_dict() for j in coordinator.state.finalizing],
        "last_job": coordinator.state.last_job.summary() if coordinator.state.last_job else None,
        "last_print_image": coordinator.last_image is not None,
        "recent_errors": list(coordinator.errors),
    }
