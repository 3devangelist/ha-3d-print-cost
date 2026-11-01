"""Actions (services) of the integration.

``fdm_printer_manager.mark_print``: mark a finished print as a success or a failure
(the printer's own verdict is not always right: a "finished" print may have come
off the plate, a "failed" one may be usable).
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import CONF_PANEL, DOMAIN, SERVICE_MARK_PRINT
from .outcome import MARKABLE

ATTR_ENTRY_ID = "entry_id"
ATTR_RESULT = "result"
ATTR_JOB_ID = "job_id"

MARK_PRINT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTRY_ID): cv.string,
        vol.Required(ATTR_RESULT): vol.In(MARKABLE),
        vol.Optional(ATTR_JOB_ID): cv.string,
    }
)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions once."""
    if hass.services.has_service(DOMAIN, SERVICE_MARK_PRINT):
        return

    async def _mark_print(call: ServiceCall) -> None:
        entry = hass.config_entries.async_get_entry(call.data[ATTR_ENTRY_ID])
        if (
            entry is None
            or entry.domain != DOMAIN
            or entry.data.get(CONF_PANEL)
            or entry.state is not ConfigEntryState.LOADED
        ):
            raise ServiceValidationError(f"{call.data[ATTR_ENTRY_ID]} is not a loaded printer of {DOMAIN}")
        await entry.runtime_data.async_mark_result(call.data[ATTR_RESULT], call.data.get(ATTR_JOB_ID))

    hass.services.async_register(DOMAIN, SERVICE_MARK_PRINT, _mark_print, schema=MARK_PRINT_SCHEMA)
