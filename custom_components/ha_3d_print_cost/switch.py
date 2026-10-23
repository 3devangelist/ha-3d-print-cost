"""``switch.<printer>_show_last_print``: dashboard toggle current / last print.

Pure display state for a card that flips between the current and the last
print, so the dashboard needs no ``input_boolean`` helper. It turns itself
off when a new print starts, so the card goes back to the running print.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import PrintCostConfigEntry, PrintCostCoordinator
from .entity import PrintCostEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrintCostConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the switch."""
    async_add_entities([ShowLastPrintSwitch(entry.runtime_data)])


class ShowLastPrintSwitch(PrintCostEntity, SwitchEntity, RestoreEntity):
    """On: the dashboard shows the last print even while one is running."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: PrintCostCoordinator) -> None:
        super().__init__(coordinator, "show_last_print", "switch")
        self._attr_is_on = False
        self._job_id = self._current_job_id()

    def _current_job_id(self) -> str | None:
        data = self.coordinator.data
        job = data.current_job if data is not None else None
        return job.job_id if job is not None else None

    async def async_added_to_hass(self) -> None:
        """Restore the last choice."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == STATE_ON

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Show the last print."""
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Show the current print."""
        self._attr_is_on = False
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        job_id = self._current_job_id()
        if job_id is not None and job_id != self._job_id:
            self._attr_is_on = False
        self._job_id = job_id
        super()._handle_coordinator_update()
