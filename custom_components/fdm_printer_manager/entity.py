"""Base entity: one device per printer."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import DOMAIN
from .coordinator import PrintCostCoordinator


class PrintCostEntity(CoordinatorEntity[PrintCostCoordinator]):
    """Common device info and naming."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PrintCostCoordinator, key: str, platform_domain: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        # Suggest "sensor.<printer>_<key>" (e.g. sensor.ender_6_current_print_total_cost).
        # HA keeps it unique and the user can still rename it.
        self.entity_id = f"{platform_domain}.{slugify(coordinator.printer_name)}_{key}"
        device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.printer_name,
            manufacturer=coordinator.manufacturer,
            model="3D printer",
        )
        if url := coordinator.configuration_url:
            device_info["configuration_url"] = url
        self._attr_device_info = device_info
