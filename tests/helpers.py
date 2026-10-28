"""Test helpers: a fake ha-bambulab printer in the registries."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

SERIAL = "0948AD000000001"

BAMBU_SENSORS = {
    "print_status": "sensor.x2d_print_status",
    "subtask_name": "sensor.x2d_task_name",
    "print_progress": "sensor.x2d_print_progress",
    "remaining_time": "sensor.x2d_remaining_time",
}


def register_bambu_printer(hass: HomeAssistant) -> str:
    """Create a bambu_lab device with entities named in French (as a real user would)."""
    bambu_entry = MockConfigEntry(domain="bambu_lab", title="X2D")
    bambu_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=bambu_entry.entry_id,
        identifiers={("bambu_lab", SERIAL)},
        name="X2D",
        manufacturer="Bambu Lab",
    )
    ent_reg = er.async_get(hass)
    for key, entity_id in BAMBU_SENSORS.items():
        object_id = entity_id.split(".", 1)[1]
        ent_reg.async_get_or_create(
            "sensor",
            "bambu_lab",
            f"{SERIAL}_{key}",
            suggested_object_id=object_id,
            device_id=device.id,
            config_entry=bambu_entry,
            translation_key=key,
        )
    return device.id
