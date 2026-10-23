"""``select.<printer>_gcode_file``: sliced file to estimate.

Options are the ``.gcode`` / ``.gcode.3mf`` files of the configured folder
(one option per sliced plate), refreshed every few seconds.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PrintCostConfigEntry, PrintCostCoordinator
from .entity import PrintCostEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrintCostConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the file selector."""
    async_add_entities([GcodeFileSelect(entry.runtime_data)])


class GcodeFileSelect(PrintCostEntity, SelectEntity):
    """Choose the file the estimate button works on."""

    def __init__(self, coordinator: PrintCostCoordinator) -> None:
        super().__init__(coordinator, "gcode_file", "select")

    @property
    def options(self) -> list[str]:
        """Files found in the G-code folder, newest first."""
        return list(self.coordinator.gcode_files)

    @property
    def current_option(self) -> str | None:
        """The chosen file."""
        return self.coordinator.selected_gcode

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Where the files are looked for."""
        return {"folder": self.coordinator.gcode_folder}

    async def async_select_option(self, option: str) -> None:
        """Remember the choice."""
        self.coordinator.async_select_gcode(option)
