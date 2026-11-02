"""Printer names: no shared hostname ("cosmos") as the name of a new printer."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fdm_printer_manager import own_device
from custom_components.fdm_printer_manager.config_flow import normalise_klipper
from custom_components.fdm_printer_manager.const import DOMAIN
from custom_components.fdm_printer_manager.naming import friendly_name

from .test_klipper_integration import printer  # noqa: F401 - printer is a fixture


def test_friendly_name() -> None:
    assert friendly_name("cosmos", "192.168.1.95") == "Elegoo Centauri Carbon"
    assert friendly_name("COSMOS.local", "192.168.1.95") == "Elegoo Centauri Carbon"
    assert friendly_name("centauri-carbon-1", "192.168.1.95") == "Elegoo Centauri Carbon"  # COSMOS 26.10+
    assert friendly_name("centauri-carbon-2", "192.168.1.97") == "Elegoo Centauri Carbon"
    assert friendly_name("mainsailos", "192.168.1.50") == "Klipper 192.168.1.50"
    assert friendly_name("", "192.168.1.50") == "Klipper 192.168.1.50"
    assert friendly_name(None, "192.168.1.50") == "Klipper 192.168.1.50"
    assert friendly_name("Ender6", "192.168.1.89") == "Ender6"


def test_new_centauri_is_not_called_cosmos() -> None:
    data = normalise_klipper({"host": "192.168.1.95", "port": 80}, "cosmos")
    assert data["printer_name"] == "Elegoo Centauri Carbon"
    typed = normalise_klipper({"host": "192.168.1.95", "port": 80, "printer_name": "Centauri"}, "cosmos")
    assert typed["printer_name"] == "Centauri"


async def test_cosmos_printer_renames_itself_but_keeps_entity_ids(hass: HomeAssistant, printer) -> None:  # noqa: F811
    """Added as "cosmos" by an older version: renamed by itself at start, the entity ids stay."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="cosmos",
        unique_id="klipper_192.168.1.95_80",
        data={"printer_type": "klipper", "printer_name": "cosmos", "host": "192.168.1.95", "port": 80},
    )
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_printer_status",
        config_entry=entry,
        suggested_object_id="cosmos_printer_status",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.title == "Elegoo Centauri Carbon"
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert device.name == "Elegoo Centauri Carbon"
    assert er.async_get(hass).async_get("sensor.cosmos_printer_status") is not None  # entity id kept
    assert hass.states.get("sensor.cosmos_printer_status").name.startswith("Elegoo Centauri Carbon")


class _NewRegistry:
    """HA 2026.10+: has async_get_device_by_identifier; async_get_device must not be used."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def async_get_device_by_identifier(self, identifier, config_entry_id):
        self.calls.append((identifier, config_entry_id))
        return "device"

    def async_get_device(self, **_kwargs):
        raise AssertionError("deprecated async_get_device called")


class _OldRegistry:
    """HA before 2026.10: only async_get_device."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def async_get_device(self, **kwargs):
        self.calls.append(kwargs)
        return "device"


def test_own_device_uses_identifier_lookup_when_available() -> None:
    registry = _NewRegistry()
    assert own_device(registry, "abc") == "device"
    assert registry.calls == [((DOMAIN, "abc"), "abc")]


def test_own_device_falls_back_on_older_ha() -> None:
    registry = _OldRegistry()
    assert own_device(registry, "abc") == "device"
    assert registry.calls == [{"identifiers": {(DOMAIN, "abc")}}]
