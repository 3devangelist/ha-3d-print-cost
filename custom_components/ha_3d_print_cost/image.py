"""``image.<printer>_last_print_image``: thumbnail of the last finished print.

A copy of the ha-bambulab cover image taken when the print ended, so it
still shows the right model once the printer has started another job.
"""

from __future__ import annotations

from homeassistant.components.image import ImageEntity, infer_image_type
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PrintCostConfigEntry, PrintCostCoordinator
from .entity import PrintCostEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrintCostConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the image entity."""
    async_add_entities([LastPrintImage(hass, entry.runtime_data)])


class LastPrintImage(PrintCostEntity, ImageEntity):
    """Thumbnail kept with the last print."""

    def __init__(self, hass: HomeAssistant, coordinator: PrintCostCoordinator) -> None:
        PrintCostEntity.__init__(self, coordinator, "last_print_image", "image")
        ImageEntity.__init__(self, hass)
        self._image: bytes | None = None
        self._sync()

    def _sync(self) -> None:
        image = self.coordinator.last_image
        if image is self._image:
            return
        self._image = image
        self._attr_content_type = (infer_image_type(image) if image else None) or "image/png"
        last = self.coordinator.state.last_job
        self._attr_image_last_updated = last.finished_at if image is not None and last is not None else None

    @property
    def available(self) -> bool:
        """Unavailable until a print has left a thumbnail."""
        return self._image is not None

    async def async_image(self) -> bytes | None:
        """The stored bytes."""
        return self._image

    @callback
    def _handle_coordinator_update(self) -> None:
        self._sync()
        super()._handle_coordinator_update()
