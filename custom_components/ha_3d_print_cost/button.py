"""Buttons of the G-code estimate.

* ``button.<printer>_estimate_gcode``: estimate the file chosen in the select;
* ``button.<printer>_refresh_gcode_files``: rescan the folder now.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import PrintCostConfigEntry, PrintCostCoordinator
from .entity import PrintCostEntity
from .gcode import GcodeError

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrintCostConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the buttons."""
    coordinator = entry.runtime_data
    async_add_entities([EstimateGcodeButton(coordinator), RefreshGcodeFilesButton(coordinator)])


class EstimateGcodeButton(PrintCostEntity, ButtonEntity):
    """Estimate the selected file; the result goes to the gcode_estimate sensor."""

    def __init__(self, coordinator: PrintCostCoordinator) -> None:
        super().__init__(coordinator, "estimate_gcode", "button")

    async def async_press(self) -> None:
        """Run the estimate."""
        coordinator = self.coordinator
        label = coordinator.selected_gcode
        entry = coordinator.gcode_files.get(label) if label else None
        if entry is None:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_gcode_selected")
        try:
            await coordinator.async_estimate(entry.path, plate=entry.plate, label=label)
        except GcodeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="invalid_gcode",
                translation_placeholders={"file": label, "error": str(err)},
            ) from err


class RefreshGcodeFilesButton(PrintCostEntity, ButtonEntity):
    """Rescan the G-code folder without waiting for the next scan."""

    def __init__(self, coordinator: PrintCostCoordinator) -> None:
        super().__init__(coordinator, "refresh_gcode_files", "button")

    async def async_press(self) -> None:
        """Rescan."""
        await self.coordinator.async_scan_gcode_files()
