"""Service actions (read-only, they return response data).

* ``get_history``: the stored past prints of a printer, with totals;
* ``estimate_cost``: cost of a sliced ``.gcode`` / ``.gcode.3mf`` before printing.

Neither writes anywhere: Spoolman and HaspelSync are only read.
"""

from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .adapters.spoolman import SpoolmanError, SpoolNotFoundError
from .const import DOMAIN
from .coordinator import PrintCostConfigEntry, PrintCostCoordinator
from .estimate import LoadedSlot, assign_spools, estimate_cost
from .gcode import GcodeError, read_gcode_file
from .history import average_power_w, history_totals
from .models import PrintResult, SpoolInfo

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
            info = await hass.async_add_executor_job(read_gcode_file, path, call.data.get(ATTR_PLATE))
        except GcodeError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_gcode",
                translation_placeholders={"file": path.name, "error": str(err)},
            ) from err

        notes: list[str] = []
        spool_ids = call.data.get(ATTR_SPOOL_IDS)
        loaded: list[LoadedSlot] = []
        if len(spool_ids or []) < len(info.filaments):
            loaded = await _loaded_slots(coordinator, notes)
        assignments = assign_spools(info.filaments, spool_ids, loaded)
        spools = await _spools(coordinator, {a.spool_id for a in assignments if a.spool_id is not None}, notes)

        if (power := call.data.get(ATTR_POWER_W)) is not None:
            power_source = "parameter"
        elif (measured := average_power_w(coordinator.state.history)) is not None:
            power, count = measured
            power_source = f"history ({count} prints)"
        else:
            power, power_source = None, None

        result = estimate_cost(
            info,
            assignments,
            spools,
            average_power_w=power,
            power_source=power_source,
            price_per_kwh=coordinator.reader.price_per_kwh(),
        )
        result.notes[:0] = notes
        _LOGGER.debug("Estimate for %s: %s", path.name, result.total_cost)
        return {"file": path.name, "printer": coordinator.printer_name, **result.as_dict(coordinator.currency)}

    hass.services.async_register(
        DOMAIN, SERVICE_GET_HISTORY, get_history, schema=GET_HISTORY_SCHEMA, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ESTIMATE_COST, estimate, schema=ESTIMATE_COST_SCHEMA, supports_response=SupportsResponse.ONLY
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


async def _loaded_slots(coordinator: PrintCostCoordinator, notes: list[str]) -> list[LoadedSlot]:
    """Spools currently in the AMS, to price filaments without spool_ids."""
    printer_id = coordinator.haspelsync_printer_id
    if coordinator.haspelsync is None or not printer_id:
        notes.append("HaspelSync not configured: spools must be given with spool_ids")
        return []
    try:
        slots = await coordinator.haspelsync.get_current_slots(printer_id)
    except Exception as err:  # noqa: BLE001 - an estimate must not fail on it
        _LOGGER.warning("HaspelSync slots unavailable for the estimate: %s", err)
        notes.append(f"HaspelSync unavailable: {err}")
        return []
    return [
        LoadedSlot(slot=s.slot, spool_id=s.spool_id, material=s.material, color=s.color)
        for s in slots.values()
        if s.spool_id is not None
    ]


async def _spools(
    coordinator: PrintCostCoordinator, spool_ids: set[int], notes: list[str]
) -> dict[int, SpoolInfo | None]:
    """Read the spools from Spoolman (missing key: could not be read)."""
    if not spool_ids:
        return {}
    if coordinator.spoolman is None:
        notes.append("Spoolman not configured: filament cost unknown")
        return {}
    spools: dict[int, SpoolInfo | None] = {}
    for spool_id in sorted(spool_ids):
        try:
            spools[spool_id] = await coordinator.spoolman.get_spool(spool_id)
        except SpoolNotFoundError:
            spools[spool_id] = None
        except SpoolmanError as err:
            _LOGGER.warning("Spoolman unavailable for the estimate: %s", err)
            notes.append(f"Spoolman unavailable: {err}")
            break
    return spools
