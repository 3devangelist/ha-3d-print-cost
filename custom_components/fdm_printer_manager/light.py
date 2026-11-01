"""``light.<printer>_light_<name>``: the printer's own lights.

Klipper: LEDs (SET_LED) and ``output_pin`` lights (SET_PIN) found on the
printer. Bambu Lab over LAN: the chamber light. A printer without lights gets
none; a light found later (the printer was off at start) is added then.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PrintCostConfigEntry, PrintCostCoordinator
from .entity import PrintCostEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrintCostConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a light entity for each light the printer has, now and when one shows up."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new = [PrinterLight(coordinator, key, name) for key, name in coordinator.own_lights.items() if key not in known]
        if new:
            known.update(light.key for light in new)
            async_add_entities(new)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class PrinterLight(PrintCostEntity, LightEntity):
    """One light, on / off."""

    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}  # noqa: RUF012 - HA reads it as a class attribute

    def __init__(self, coordinator: PrintCostCoordinator, key: str, name: str) -> None:
        super().__init__(coordinator, key, "light")
        self.key = key
        self._attr_translation_key = "printer_light"
        self._attr_translation_placeholders = {"light": name}

    @property
    def available(self) -> bool:
        """Unavailable when the printer does not answer."""
        return self.coordinator.reachable and self.coordinator.light_is_on(self.key) is not None

    @property
    def is_on(self) -> bool | None:
        """As last read from the printer."""
        return self.coordinator.light_is_on(self.key)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on (Klipper: as it was last lit)."""
        await self.coordinator.async_set_light(self.key, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        await self.coordinator.async_set_light(self.key, False)
