"""Base entity: one virtual device per printer, linked to the Bambu device."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import BAMBU_DOMAIN, DOMAIN, NAME
from .coordinator import PrintCostCoordinator


class PrintCostEntity(CoordinatorEntity[PrintCostCoordinator]):
    """Common device info and naming."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PrintCostCoordinator, key: str, platform_domain: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        # Suggest "sensor.<printer>_<key>" (e.g. sensor.x2d_current_print_total_cost).
        # HA keeps it unique and the user can still rename it.
        self.entity_id = f"{platform_domain}.{slugify(coordinator.printer_name)}_{key}"
        device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"{NAME} — {coordinator.printer_name}",
            manufacturer="HA 3D Print Cost",
            model="Print cost tracker",
        )
        if coordinator.printer_serial:
            device_info["via_device"] = (BAMBU_DOMAIN, coordinator.printer_serial)
        self._attr_device_info = device_info
