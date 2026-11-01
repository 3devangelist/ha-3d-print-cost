"""``switch.<printer>_show_last_print``: dashboard toggle current / last print.

Pure display state for a card that flips between the current and the last
print, so the dashboard needs no ``input_boolean`` helper. It turns itself
off when a new print starts, so the card goes back to the running print.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON, EntityCategory
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
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
    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = [ShowLastPrintSwitch(coordinator)]
    if coordinator.switch_entity:
        entities.append(SafePowerSwitch(coordinator))
    async_add_entities(entities)


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


class SafePowerSwitch(PrintCostEntity, SwitchEntity):
    """``switch.<printer>_safe_power``: the printer's plug, turned off safely.

    On: turns the plug on. Off: refused while printing or paused; Klipper
    printers shut their computer down first and the plug is cut once it is
    down (at most 3 minutes later); other printers are cut right away.
    Can be put on any dashboard and used in automations.
    """

    def __init__(self, coordinator: PrintCostCoordinator) -> None:
        super().__init__(coordinator, "safe_power", "switch")

    async def async_added_to_hass(self) -> None:
        """Follow the plug's own state."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(self.hass, [self.coordinator.switch_entity], self._on_plug_change)
        )

    @callback
    def _on_plug_change(self, event: Event[EventStateChangedData]) -> None:
        self.async_write_ha_state()

    @property
    def _plug_state(self):
        return self.hass.states.get(self.coordinator.switch_entity) if self.hass else None

    @property
    def available(self) -> bool:
        """Unavailable when the plug itself is."""
        state = self._plug_state
        return state is not None and state.state not in ("unavailable", "unknown")

    @property
    def is_on(self) -> bool | None:
        """On while the plug is on (including while it waits to be cut)."""
        state = self._plug_state
        return None if state is None else state.state == STATE_ON

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Shutdown progress, for the panel and dashboards."""
        cut_at = self.coordinator.power_cut_at
        return {
            "phase": self.coordinator.power_phase or ("on" if self.is_on else "off"),
            "cut_at": cut_at.isoformat() if cut_at else None,
            "printing": self.coordinator.is_printing,
            "plug": self.coordinator.switch_entity,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the plug on."""
        await self.coordinator.async_power_on()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Safe power off (refused while printing)."""
        await self.coordinator.async_power_off()
