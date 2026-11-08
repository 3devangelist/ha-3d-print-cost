"""Actions (services) of the integration.

``fdm_printer_manager.mark_print``: mark a finished print as a success or a failure
(the printer's own verdict is not always right: a "finished" print may have come
off the plate, a "failed" one may be usable).

``fdm_printer_manager.babystep``: move the nozzle up or down while a Klipper
printer prints (live Z offset), reset that offset, or keep it.

``fdm_printer_manager.sheets_setup``: log every printer (or one) to a tab of a
Google Sheets integration entry; the tab is created when it is missing.

``fdm_printer_manager.keep_on`` (0.15.0): keep the printer on, no auto-off this time.

``fdm_printer_manager.force_power_off`` (0.15.0): cut the plug now, without cooling
the nozzle or shutting the computer down. Only with ``confirm: true``.

``fdm_printer_manager.set_material`` (0.15.0): the material of a finished print, chosen
by hand; its filament cost is worked out again.

``mark_print``, ``force_power_off``, ``set_material`` and ``sheets_setup`` change the print log and the options of
every printer, so only administrators may call them (automations and scripts
still can: they run without a user). Home Assistant has no per-action
permission: "users the admin allows" means users the admin makes admin.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service

from .adapters.klipper_extras import BABYSTEP_STEPS
from .const import (
    CONF_PANEL,
    DOMAIN,
    SERVICE_BABYSTEP,
    SERVICE_FORCE_POWER_OFF,
    SERVICE_KEEP_ON,
    SERVICE_MARK_PRINT,
    SERVICE_SET_MATERIAL,
    SERVICE_SHEETS_SETUP,
)
from .materials import MATERIALS
from .outcome import MARKABLE
from .sheets import async_setup_log

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

ATTR_CONFIRM = "confirm"
ATTR_MATERIAL = "material"

ENTRY_SCHEMA = vol.Schema({vol.Required(ATTR_ENTRY_ID): cv.string})

# confirm must be given and true: the plug is cut without cooling the nozzle or shutting the computer down.
FORCE_POWER_OFF_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTRY_ID): cv.string,
        vol.Required(ATTR_CONFIRM): vol.All(cv.boolean, vol.IsTrue("confirm must be true")),
    }
)

SET_MATERIAL_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTRY_ID): cv.string,
        vol.Required(ATTR_JOB_ID): cv.string,
        vol.Required(ATTR_MATERIAL): vol.In(MATERIALS),
    }
)

ATTR_CONFIG_ENTRY = "config_entry"
ATTR_WORKSHEET = "worksheet"

SHEETS_SETUP_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY): cv.string,
        vol.Optional(ATTR_WORKSHEET): cv.string,
        vol.Optional(ATTR_ENTRY_ID): vol.All(cv.ensure_list, [cv.string]),
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

    async_register_admin_service(hass, DOMAIN, SERVICE_MARK_PRINT, _mark_print, schema=MARK_PRINT_SCHEMA)

    async def _sheets_setup(call: ServiceCall) -> ServiceResponse:
        return await async_setup_log(
            hass,
            call.data.get(ATTR_CONFIG_ENTRY),
            call.data.get(ATTR_WORKSHEET),
            call.data.get(ATTR_ENTRY_ID) or None,
        )

    hass.services.async_register(DOMAIN, SERVICE_BABYSTEP, _babystep, schema=BABYSTEP_SCHEMA)

    async def _keep_on(call: ServiceCall) -> ServiceResponse:
        done = await _printer(hass, call.data[ATTR_ENTRY_ID]).async_keep_on()
        return {"done": done}

    async def _force_power_off(call: ServiceCall) -> None:
        await _printer(hass, call.data[ATTR_ENTRY_ID]).async_force_power_off(confirm=call.data[ATTR_CONFIRM])

    async def _set_material(call: ServiceCall) -> None:
        await _printer(hass, call.data[ATTR_ENTRY_ID]).async_set_material(
            call.data[ATTR_JOB_ID], call.data[ATTR_MATERIAL]
        )

    hass.services.async_register(
        DOMAIN, SERVICE_KEEP_ON, _keep_on, schema=ENTRY_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    async_register_admin_service(hass, DOMAIN, SERVICE_FORCE_POWER_OFF, _force_power_off, schema=FORCE_POWER_OFF_SCHEMA)
    async_register_admin_service(hass, DOMAIN, SERVICE_SET_MATERIAL, _set_material, schema=SET_MATERIAL_SCHEMA)
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_SHEETS_SETUP,
        _sheets_setup,
        schema=SHEETS_SETUP_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
