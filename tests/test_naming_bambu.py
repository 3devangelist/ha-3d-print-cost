"""Printers name themselves: Bambu Lab by model, shared hostnames renamed on existing installs."""

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fdm_printer_manager.adapters.moonraker import FileMetadata, KlipperStatus
from custom_components.fdm_printer_manager.const import DOMAIN
from custom_components.fdm_printer_manager.naming import bambu_model, bambu_name, is_generic
from custom_components.fdm_printer_manager.photo import normalise_fields

CLIENT = "custom_components.fdm_printer_manager.coordinator.MoonrakerClient"


def test_bambu_model_from_serial_or_code() -> None:
    assert bambu_model("0309CA123456789") == "A1 mini"
    assert bambu_model("039XYZ123456789") == "A1"
    assert bambu_model("01P00A123456789") == "P1S"
    assert bambu_model("00M00A123456789") == "X1 Carbon"
    assert bambu_model(None, "N1") == "A1 mini"
    assert bambu_model("ZZZ", "C12") == "P1S"
    assert bambu_model("ZZZ000000000000") is None


def test_bambu_name() -> None:
    assert bambu_name("0309CA123456789") == "Bambu Lab A1 mini"
    assert bambu_name("ZZZ000000001234", name="micromachine") == "Bambu 1234"  # a shared hostname is no name
    assert bambu_name("ZZZ000000001234", name="Workshop") == "Workshop"
    assert bambu_name("0309CA123456789", name="micromachine") == "Bambu Lab A1 mini"


def test_generic_names() -> None:
    assert is_generic("micromachine")
    assert is_generic("cosmos")
    assert is_generic("MainsailOS.local")
    assert is_generic("")
    assert not is_generic("Ender 6")


def test_photo_fills_the_name_from_the_serial() -> None:
    result = normalise_fields({"ip": "192.168.1.96", "serial": "0309CA123456789", "access_code": "12345678"})
    assert result["fields"]["printer_name"] == "Bambu Lab A1 mini"
    # Only IP and code (the A1 mini's LAN Only screen): no name guessed
    result = normalise_fields({"ip": "192.168.1.96", "access_code": "12345678"})
    assert "printer_name" not in result["fields"]


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, *, add: bool = True) -> None:
    if add:
        entry.add_to_hass(hass)
    with patch.multiple(
        CLIENT,
        status=AsyncMock(return_value=KlipperStatus(state="standby")),
        metadata=AsyncMock(return_value=FileMetadata()),
        webcams=AsyncMock(return_value=[]),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


def _klipper(title: str, entry_id: str = "abc") -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=title,
        entry_id=entry_id,
        unique_id=f"klipper_192.168.1.95_80_{entry_id}",
        data={"printer_type": "klipper", "printer_name": title, "host": "192.168.1.95", "port": 80},
    )


async def test_generic_klipper_hostname_is_renamed(hass: HomeAssistant) -> None:
    entry = _klipper("mainsailos")
    await _setup(hass, entry)
    assert entry.title == "Klipper 192.168.1.95"
    assert entry.data["printer_name"] == "Klipper 192.168.1.95"


async def test_bambu_lan_micromachine_is_renamed_to_its_model(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="micromachine",
        data={
            "printer_type": "bambu_lan",
            "printer_name": "micromachine",
            "host": "192.168.1.96",
            "printer_serial": "0309CA123456789",
            "access_code": "12345678",
        },
    )
    entry.add_to_hass(hass)
    with patch("custom_components.fdm_printer_manager.coordinator.BambuLanClient.start"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.title == "Bambu Lab A1 mini"
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert device.name == "Bambu Lab A1 mini"


async def test_name_given_by_the_user_is_kept(hass: HomeAssistant) -> None:
    entry = _klipper("cosmos", "def")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "def")}, name="cosmos"
    )
    dr.async_get(hass).async_update_device(device.id, name_by_user="Centauri AFS")
    await _setup(hass, entry, add=False)
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "def")})
    assert device.name_by_user == "Centauri AFS"


async def test_a_real_name_is_not_touched(hass: HomeAssistant) -> None:
    entry = _klipper("Ender 6", "ghi")
    await _setup(hass, entry)
    assert entry.title == "Ender 6"
