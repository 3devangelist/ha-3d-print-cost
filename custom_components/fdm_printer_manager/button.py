"""Buttons of a printer.

``button.<printer>_pause_print``, ``_resume_print`` and ``_cancel_print``:
available only when the action makes sense (e.g. Resume while paused).
Klipper: Moonraker commands. Bambu Lab: the matching ha-bambulab buttons.

0.15.0, only for a printer with a smart plug:

* ``button.<printer>_keep_on``: cancels the auto-off (while it waits or the
  nozzle cools), or, during a print, keeps the printer on after it. The next
  print turns it off again as usual.
* ``button.<printer>_power_off_now``: cuts the plug at once, without cooling the
  nozzle or shutting the computer down (a printer or Moonraker that hangs). It
  has to be pressed 3 times, each within 5 seconds of the last; the count is in
  the attributes. A running print is closed as failed by the user.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import FORCE_OFF_PRESSES, FORCE_OFF_WINDOW
from .coordinator import PrintCostConfigEntry, PrintCostCoordinator
from .entity import PrintCostEntity

PARALLEL_UPDATES = 1

ACTIONS = ("pause", "resume", "cancel")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrintCostConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the print control buttons (and the plug buttons when there is a plug)."""
    coordinator = entry.runtime_data
    buttons: list[ButtonEntity] = [PrintControlButton(coordinator, action) for action in ACTIONS]
    if coordinator.switch_entity:
        buttons += [KeepOnButton(coordinator), PowerOffNowButton(coordinator)]
    async_add_entities(buttons)


class PrintControlButton(PrintCostEntity, ButtonEntity):
    """One print control."""

    def __init__(self, coordinator: PrintCostCoordinator, action: str) -> None:
        super().__init__(coordinator, f"{action}_print", "button")
        self._action = action

    @property
    def available(self) -> bool:
        """Only when the printer is in a state where this action applies."""
        return super().available and self.coordinator.can_control(self._action)

    async def async_press(self) -> None:
        """Send the command to the printer."""
        await self.coordinator.async_control(self._action)


class KeepOnButton(PrintCostEntity, ButtonEntity):
    """Keep the printer on: no auto-off this time."""

    def __init__(self, coordinator: PrintCostCoordinator) -> None:
        super().__init__(coordinator, "keep_on", "button")

    async def async_press(self) -> None:
        """Cancel the pending auto-off (or the one after the running print)."""
        await self.coordinator.async_keep_on()


class PowerOffNowButton(PrintCostEntity, ButtonEntity):
    """Cut the plug at once, after 3 presses in a row."""

    def __init__(self, coordinator: PrintCostCoordinator) -> None:
        super().__init__(coordinator, "power_off_now", "button")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """How many presses were counted, and how many cut the plug."""
        return {
            "presses": self.coordinator.force_presses,
            "presses_needed": FORCE_OFF_PRESSES,
            "window_s": FORCE_OFF_WINDOW,
        }

    async def async_press(self) -> None:
        """Count the press; the third one in a row cuts the plug."""
        await self.coordinator.async_force_off_press()
