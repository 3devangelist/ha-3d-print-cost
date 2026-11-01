"""Pause / resume / cancel the print from Home Assistant.

``button.<printer>_pause_print``, ``_resume_print`` and ``_cancel_print``:
available only when the action makes sense (e.g. Resume while paused).
Klipper: Moonraker commands. Bambu Lab: the matching ha-bambulab buttons.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PrintCostConfigEntry, PrintCostCoordinator
from .entity import PrintCostEntity

PARALLEL_UPDATES = 1

ACTIONS = ("pause", "resume", "cancel")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrintCostConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the print control buttons."""
    async_add_entities(PrintControlButton(entry.runtime_data, action) for action in ACTIONS)


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
