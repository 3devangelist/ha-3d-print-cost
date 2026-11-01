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

from .const import SAFE_POWER_COOL_TEMP, SAFE_POWER_WAIT
from .coordinator import LEFT_ON_RESULTS, PrintCostConfigEntry, PrintCostCoordinator
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

    On: turns the plug on (and cancels a pending auto-off). Off: refused while
    printing or paused; with the nozzle at 80 °C or more it waits for it to
    cool first (phase ``cooling``, at most 30 minutes, then it gives up and
    leaves the plug on); Klipper printers then shut their computer down
    (Moonraker, else SSH) and the plug is cut once the computer stops
    answering (phase ``shutting_down``; still answering after 5 minutes, the
    plug is left on); other printers are cut right away.
    After a print ends, the same happens by itself ``auto_off_minutes`` later
    (phase ``waiting``). Can be put on any dashboard and used in automations.
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
        old, new = event.data["old_state"], event.data["new_state"]
        coordinator = self.coordinator
        if (
            old is not None
            and new is not None
            and {old.state, new.state} == {"on", "off"}
            and coordinator.power_phase is None
            and coordinator.power_result in LEFT_ON_RESULTS
        ):
            # The plug was switched since: why the last power off left it on no longer applies.
            coordinator.power_result = None
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
        coordinator = self.coordinator
        cut_at = coordinator.power_cut_at
        auto_off_at = coordinator.auto_off_at
        return {
            "phase": coordinator.power_phase or ("on" if self.is_on else "off"),
            "cut_at": cut_at.isoformat() if cut_at else None,
            "auto_off_at": auto_off_at.isoformat() if auto_off_at else None,
            "auto_off_minutes": coordinator.auto_off_minutes,
            "last_result": coordinator.power_result,
            "ssh": coordinator.ssh_login is not None,
            "cool_to": SAFE_POWER_COOL_TEMP,
            "shutdown_wait_minutes": SAFE_POWER_WAIT // 60,
            "printing": coordinator.is_printing,
            "plug": coordinator.switch_entity,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the plug on."""
        await self.coordinator.async_power_on()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Safe power off (refused while printing)."""
        await self.coordinator.async_power_off()
