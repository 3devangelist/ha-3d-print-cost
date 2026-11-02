"""Diagnostics (Settings → Devices → … → Download diagnostics)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import (
    CONF_ACCESS_CODE,
    CONF_API_KEY,
    CONF_HOST,
    CONF_NOTIFY,
    CONF_PANEL,
    CONF_PRINTER_DEVICE_ID,
    CONF_PRINTER_SERIAL,
    CONF_PRINTER_TYPE,
    CONF_SSH_COMMAND,
    CONF_SSH_KEY,
    CONF_SSH_PASSWORD,
    CONF_SSH_USERNAME,
    CONF_WEB_URL,
    DOMAIN,
)
from .coordinator import PrintCostConfigEntry

# Secrets, and what identifies the printer or the network (diagnostics end up in public issues).
TO_REDACT = {
    CONF_API_KEY,
    CONF_ACCESS_CODE,
    CONF_SSH_PASSWORD,
    CONF_SSH_KEY,
    CONF_SSH_USERNAME,
    CONF_SSH_COMMAND,
    CONF_PRINTER_SERIAL,
    CONF_PRINTER_DEVICE_ID,
    CONF_HOST,
    CONF_WEB_URL,
    CONF_NOTIFY,
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: PrintCostConfigEntry) -> dict[str, Any]:
    """Return redacted diagnostics."""
    integration = await async_get_integration(hass, DOMAIN)
    if entry.data.get(CONF_PANEL):
        return _hub_diagnostics(hass, entry, str(integration.version))
    coordinator = entry.runtime_data

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
            "reachable": (coordinator.klipper or coordinator.bambu_lan).available
            if (coordinator.klipper or coordinator.bambu_lan)
            else None,
            "last_reading": str((coordinator.klipper or coordinator.bambu_lan).reading)
            if (coordinator.klipper or coordinator.bambu_lan)
            else None,
            # Which identities are remembered and whether one changed; never the values themselves.
            "identity_known": sorted(coordinator.identity.known),
            "identity_changed": sorted(coordinator.identity.changes),
        },
        "active_job": active.as_dict() if active else None,
        "finalizing_jobs": [j.as_dict() for j in coordinator.state.finalizing],
        "last_job": coordinator.state.last_job.summary() if coordinator.state.last_job else None,
        "last_print_image": coordinator.last_image is not None,
        # Errors can quote the printer's URL: the host is taken out.
        "recent_errors": [_hide_host(e, coordinator.conf.get(CONF_HOST)) for e in coordinator.errors],
    }


def _hub_diagnostics(hass: HomeAssistant, entry: ConfigEntry, version: str) -> dict[str, Any]:
    """The "FDM Printers" hub entry: it only holds the sidebar panel, so it has no coordinator.

    Shows the panel and the background services, and the printers by type and state (no names,
    hosts or other values that identify a printer).
    """
    data = hass.data.get(DOMAIN, {})
    printers = [
        {
            "type": str((e.data | e.options).get(CONF_PRINTER_TYPE) or "?"),
            "state": str(e.state.value),
            "disabled": e.disabled_by is not None,
        }
        for e in hass.config_entries.async_entries(DOMAIN)
        if not e.data.get(CONF_PANEL)
    ]
    return {
        "version": version,
        "entry": "hub",
        "config": async_redact_data({**entry.data, **entry.options}, TO_REDACT),
        "panel_registered": bool(data.get("panel")),
        "websocket_commands": bool(data.get("ws")),
        "network_scan": data.get("scan_stop") is not None,
        "bambu_ssdp": bool(data.get("ssdp")),
        "printer_count": len(printers),
        "printers": printers,
    }


def _hide_host(text: str, host: str | None) -> str:
    """The printer's address replaced by **REDACTED** (it can sit in error messages)."""
    host = str(host or "").strip()
    return text.replace(host, "**REDACTED**") if host else text
