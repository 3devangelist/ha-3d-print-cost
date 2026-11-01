"""What happens when a print ends, besides the event: a to-do item and / or the user's script.

* **To-do**: "Remove the print from <printer>" on the chosen list, so a finished
  part is not forgotten on the plate (and the next print does not start on it).
* **Script**: started with the print summary as variables (``printer``, ``name``,
  ``result``, ``energy_kwh``, ``electricity_cost``, ``currency``...), for anything else.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .const import CONF_END_SCRIPT, CONF_TODO_ENTITY

_LOGGER = logging.getLogger(__name__)

RESULT_TEXT = {"success": "finished", "failed": "failed", "cancelled": "cancelled"}


def todo_item(payload: dict[str, Any]) -> tuple[str, str]:
    """Summary and description of the to-do item."""
    printer = payload.get("printer") or "the printer"
    name = payload.get("name") or "print"
    result = RESULT_TEXT.get(payload.get("result") or "", "ended")
    return f"Remove the print from {printer}", f"{name} — {result}"


async def async_run_end_actions(hass: HomeAssistant, conf: dict[str, Any], payload: dict[str, Any]) -> None:
    """Add the to-do item and start the script; a failure is logged, never raised."""
    if todo := conf.get(CONF_TODO_ENTITY):
        summary, description = todo_item(payload)
        try:
            await hass.services.async_call(
                "todo",
                "add_item",
                {"item": summary, "description": description},
                target={"entity_id": todo},
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001 - the print is stored anyway
            _LOGGER.warning("%s: cannot add the to-do item to %s: %s", payload.get("printer"), todo, err)
    if script := conf.get(CONF_END_SCRIPT):
        try:
            await hass.services.async_call(
                "script", "turn_on", {"entity_id": script, "variables": payload}, blocking=False
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("%s: cannot start %s: %s", payload.get("printer"), script, err)
