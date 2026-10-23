"""Service actions.

* ``get_history``: the stored past prints of a printer, with totals;
* ``estimate_cost``: cost of a sliced ``.gcode`` / ``.gcode.3mf`` before
  printing; the result is also kept for the ``gcode_estimate`` sensor.

Spoolman and HaspelSync are only read.
"""

from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .coordinator import PrintCostConfigEntry, PrintCostCoordinator
from .gcode import GcodeError
from .history import history_totals
from .models import PrintResult

_LOGGER = logging.getLogger(__name__)

SERVICE_GET_HISTORY = "get_history"
SERVICE_ESTIMATE_COST = "estimate_cost"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_LIMIT = "limit"
ATTR_RESULT = "result"
ATTR_FILE_PATH = "file_path"
ATTR_PLATE = "plate"
ATTR_SPOOL_IDS = "spool_ids"
ATTR_POWER_W = "power_w"

GET_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_LIMIT): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Optional(ATTR_RESULT): vol.In([str(r) for r in PrintResult]),
    }
)

ESTIMATE_COST_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_FILE_PATH): cv.string,
        vol.Optional(ATTR_PLATE): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Optional(ATTR_SPOOL_IDS): vol.All(cv.ensure_list, [vol.Coerce(int)]),
        vol.Optional(ATTR_POWER_W): vol.All(vol.Coerce(float), vol.Range(min=1, max=10000)),
    }
)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions (once, from ``async_setup``)."""

    async def get_history(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        jobs = list(coordinator.state.history)  # newest first
        if result := call.data.get(ATTR_RESULT):
            jobs = [j for j in jobs if j.get("result") == result]
        if limit := call.data.get(ATTR_LIMIT):
            jobs = jobs[:limit]
        return {
            "printer": coordinator.printer_name,
            "currency": coordinator.currency,
            "totals": history_totals(jobs),
            "jobs": jobs,
        }

    async def estimate(call: ServiceCall) -> ServiceResponse:
        coordinator = _coordinator(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        path = _resolve_path(hass, call.data[ATTR_FILE_PATH])
        if not await hass.async_add_executor_job(hass.config.is_allowed_path, str(path)):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="path_not_allowed",
                translation_placeholders={"path": str(path)},
            )
        try:
            response = await coordinator.async_estimate(
                path,
                plate=call.data.get(ATTR_PLATE),
                spool_ids=call.data.get(ATTR_SPOOL_IDS),
                power_w=call.data.get(ATTR_POWER_W),
            )
        except GcodeError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_gcode",
                translation_placeholders={"file": path.name, "error": str(err)},
            ) from err
        return response if call.return_response else None

    hass.services.async_register(
        DOMAIN, SERVICE_GET_HISTORY, get_history, schema=GET_HISTORY_SCHEMA, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ESTIMATE_COST,
        estimate,
        schema=ESTIMATE_COST_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _coordinator(hass: HomeAssistant, entry_id: str | None) -> PrintCostCoordinator:
    """The targeted printer; optional when only one is configured."""
    if entry_id:
        entry: PrintCostConfigEntry | None = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entry_not_found",
                translation_placeholders={"entry_id": entry_id},
            )
        if entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entry_not_loaded",
                translation_placeholders={"title": entry.title},
            )
        return entry.runtime_data

    loaded = [e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]
    if not loaded:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_entry")
    if len(loaded) > 1:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="several_entries")
    return loaded[0].runtime_data


def _resolve_path(hass: HomeAssistant, file_path: str) -> Path:
    """Absolute path; relative ones are taken from the HA config directory."""
    path = Path(file_path.strip())
    return path if path.is_absolute() else Path(hass.config.path(str(path)))
