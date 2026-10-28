"""``event.<printer>_print_finished``: fired once per finished print.

The same payload is also fired on the bus as ``printer_manager_print_finished``.
"""

from __future__ import annotations

from typing import Any, ClassVar

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import EVENT_TYPE_PRINT_FINISHED
from .coordinator import PrintCostConfigEntry, PrintCostCoordinator
from .entity import PrintCostEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrintCostConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the event entity."""
    async_add_entities([PrintFinishedEvent(entry.runtime_data)])


class PrintFinishedEvent(PrintCostEntity, EventEntity):
    """Triggered when a print has been fully costed."""

    _attr_event_types: ClassVar[list[str]] = [EVENT_TYPE_PRINT_FINISHED]

    def __init__(self, coordinator: PrintCostCoordinator) -> None:
        super().__init__(coordinator, "print_finished", "event")

    async def async_added_to_hass(self) -> None:
        """Subscribe to finished prints."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_finished_listener(self._on_finished))

    @callback
    def _on_finished(self, payload: dict[str, Any]) -> None:
        attrs = {k: v for k, v in payload.items() if k not in ("notes",)}
        self._trigger_event(EVENT_TYPE_PRINT_FINISHED, attrs)
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Ignore regular updates: the state only changes on events."""
