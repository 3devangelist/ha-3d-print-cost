"""Actions (services) of the integration.

``fdm_printer_manager.mark_print``: mark a finished print as a success or a failure
(the printer's own verdict is not always right: a "finished" print may have come
off the plate, a "failed" one may be usable).

``fdm_printer_manager.babystep``: move the nozzle up or down while a Klipper
printer prints (live Z offset), reset that offset, or keep it.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .adapters.klipper_extras import BABYSTEP_STEPS
from .const import CONF_PANEL, DOMAIN, SERVICE_BABYSTEP, SERVICE_MARK_PRINT
from .outcome import MARKABLE

ATTR_ENTRY_ID = "entry_id"
ATTR_RESULT = "result"
ATTR_JOB_ID = "job_id"
ATTR_DIRECTION = "direction"
ATTR_STEP = "step"
ATTR_RESET = "reset"
ATTR_SAVE = "save"

MARK_PRINT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTRY_ID): cv.string,
        vol.Required(ATTR_RESULT): vol.In(MARKABLE),
        vol.Optional(ATTR_JOB_ID): cv.string,
    }
)

BABYSTEP_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTRY_ID): cv.string,
        vol.Optional(ATTR_DIRECTION): vol.In(["up", "down"]),
        vol.Optional(ATTR_STEP, default=0.01): vol.All(vol.Coerce(float), vol.In(BABYSTEP_STEPS)),
        vol.Optional(ATTR_RESET, default=False): cv.boolean,
        vol.Optional(ATTR_SAVE, default=False): cv.boolean,
    }
)


def _printer(hass: HomeAssistant, entry_id: str):
    entry = hass.config_entries.async_get_entry(entry_id)
    if (
        entry is None
        or entry.domain != DOMAIN
        or entry.data.get(CONF_PANEL)
        or entry.state is not ConfigEntryState.LOADED
    ):
        raise ServiceValidationError(f"{entry_id} is not a loaded printer of {DOMAIN}")
    return entry.runtime_data


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions once."""
    if hass.services.has_service(DOMAIN, SERVICE_MARK_PRINT):
        return

    async def _mark_print(call: ServiceCall) -> None:
        await _printer(hass, call.data[ATTR_ENTRY_ID]).async_mark_result(
            call.data[ATTR_RESULT], call.data.get(ATTR_JOB_ID)
        )

    async def _babystep(call: ServiceCall) -> None:
        coordinator = _printer(hass, call.data[ATTR_ENTRY_ID])
        direction = call.data.get(ATTR_DIRECTION)
        reset, save = call.data[ATTR_RESET], call.data[ATTR_SAVE]
        if not (reset or save or direction):
            raise ServiceValidationError("Give a direction (up / down), reset or save")
        # "Up" raises the nozzle (more gap), "down" brings it closer to the bed.
        delta = call.data[ATTR_STEP] * (1 if direction == "up" else -1)
        await coordinator.async_babystep(delta, reset=reset, save=save)

    hass.services.async_register(DOMAIN, SERVICE_MARK_PRINT, _mark_print, schema=MARK_PRINT_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_BABYSTEP, _babystep, schema=BABYSTEP_SCHEMA)
