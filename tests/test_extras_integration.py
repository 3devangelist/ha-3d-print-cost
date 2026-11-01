"""Lights, baby steps and filaments end to end, with the real Centauri Carbon ("cosmos") answers."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fdm_printer_manager.adapters.klipper_extras import discover
from custom_components.fdm_printer_manager.adapters.moonraker import FileMetadata, KlipperStatus
from custom_components.fdm_printer_manager.const import DOMAIN

CLIENT = "custom_components.fdm_printer_manager.coordinator.MoonrakerClient"
FIXTURES = Path(__file__).parent / "fixtures"
OBJECTS = json.loads((FIXTURES / "centauri_cosmos_objects.json").read_text("utf-8"))["objects"]
STATUS = json.loads((FIXTURES / "centauri_cosmos_status.json").read_text("utf-8"))
HELP = json.loads((FIXTURES / "gcode_help.json").read_text("utf-8"))["centauri"]


class FakeCentauri:
    """Answers like the real printer; ``state`` changes between polls."""

    def __init__(self) -> None:
        self.state = "standby"
        self.gcode = AsyncMock()

    async def status(self, extra_objects=()) -> KlipperStatus:
        reading = KlipperStatus(state=self.state, nozzle_temp=26.0, klippy_state="ready")
        reading.extras = {k: v for k, v in STATUS.items() if k in extra_objects}
        return reading


@pytest.fixture
def centauri():
    fake = FakeCentauri()
    with patch.multiple(
        CLIENT,
        status=AsyncMock(side_effect=fake.status),
        features=AsyncMock(return_value=discover(OBJECTS, HELP["commands"], HELP["components"])),
        gcode=fake.gcode,
        metadata=AsyncMock(return_value=FileMetadata()),
        webcams=AsyncMock(return_value=[]),
    ):
        yield fake


async def _setup(hass: HomeAssistant, **options) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Centauri",
        unique_id="klipper_192.168.1.95_80",
        data={"printer_type": "klipper", "printer_name": "Centauri", "host": "192.168.1.95", "port": 80},
        options=options,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_card_extras_from_the_real_centauri(hass: HomeAssistant, centauri: FakeCentauri) -> None:
    entry = await _setup(hass)
    entry.runtime_data.async_update_listeners()  # next poll: the light entities now exist
    await hass.async_block_till_done()
    attrs = hass.states.get("sensor.centauri_printer_status").attributes
    assert [light["name"] for light in attrs["lights"]] == ["Case", "Hotend"]
    assert attrs["lights"][0]["entity"] == "light.centauri_light_case"
    assert [slot["material"] for slot in attrs["filaments"]] == ["PETG", None, None, None]
    assert attrs["filaments"][0]["color"] == "#FFFFFF"
    assert attrs["babystep"] == {"z_offset": 0.0, "steps": [0.005, 0.01, 0.025, 0.05], "limit": 1.0, "save": False}

    assert hass.states.get("light.centauri_light_case").state == "on"
    assert hass.states.get("light.centauri_light_hotend").state == "off"
    assert hass.states.get("sensor.centauri_filaments").state == "1/4 loaded"
    assert hass.states.get("sensor.centauri_filaments").attributes["slots"][0]["remaining"] == 79.2
    assert hass.states.get("sensor.centauri_z_offset").state == "0.0"


async def test_light_switches_with_set_led(hass: HomeAssistant, centauri: FakeCentauri) -> None:
    await _setup(hass)
    await hass.services.async_call("light", "turn_off", {"entity_id": "light.centauri_light_case"}, blocking=True)
    centauri.gcode.assert_awaited_with("SET_LED LED=case RED=0 GREEN=0 BLUE=0 WHITE=0")
    await hass.services.async_call("light", "turn_on", {"entity_id": "light.centauri_light_case"}, blocking=True)
    # Back as it was before: white only
    centauri.gcode.assert_awaited_with("SET_LED LED=case RED=0 GREEN=0 BLUE=0 WHITE=1")


async def test_baby_steps_only_while_printing(hass: HomeAssistant, centauri: FakeCentauri) -> None:
    entry = await _setup(hass)
    data = {"entry_id": entry.entry_id, "direction": "down", "step": 0.01}
    with pytest.raises(HomeAssistantError, match="only while printing"):
        await hass.services.async_call(DOMAIN, "babystep", data, blocking=True)
    centauri.gcode.assert_not_awaited()

    centauri.state = "printing"
    await entry.runtime_data.klipper.async_update()
    entry.runtime_data._process(entry.runtime_data.data.now)  # a poll
    await hass.services.async_call(DOMAIN, "babystep", data, blocking=True)
    centauri.gcode.assert_awaited_with("SET_GCODE_OFFSET Z_ADJUST=-0.010 MOVE=1")
    await hass.services.async_call(DOMAIN, "babystep", {**data, "direction": "up", "step": "0.05"}, blocking=True)
    centauri.gcode.assert_awaited_with("SET_GCODE_OFFSET Z_ADJUST=+0.050 MOVE=1")
    await hass.services.async_call(DOMAIN, "babystep", {"entry_id": entry.entry_id, "reset": True}, blocking=True)
    centauri.gcode.assert_awaited_with("SET_GCODE_OFFSET Z=0 MOVE=1")
    # Saving is off by default
    with pytest.raises(HomeAssistantError, match="turned off"):
        await hass.services.async_call(DOMAIN, "babystep", {"entry_id": entry.entry_id, "save": True}, blocking=True)


async def test_save_when_turned_on(hass: HomeAssistant, centauri: FakeCentauri) -> None:
    entry = await _setup(hass, babystep_save=True)
    await hass.services.async_call(DOMAIN, "babystep", {"entry_id": entry.entry_id, "save": True}, blocking=True)
    centauri.gcode.assert_awaited_with("Z_OFFSET_APPLY_PROBE")


async def test_baby_steps_can_be_turned_off(hass: HomeAssistant, centauri: FakeCentauri) -> None:
    await _setup(hass, babystep=False)
    assert hass.states.get("sensor.centauri_printer_status").attributes["babystep"] is None
    assert hass.states.get("sensor.centauri_z_offset") is None


async def test_printer_without_extras_shows_nothing(hass: HomeAssistant) -> None:
    # conftest: no lights, no baby steps, no filament unit
    with patch.multiple(
        CLIENT,
        status=AsyncMock(return_value=KlipperStatus(state="standby")),
        metadata=AsyncMock(return_value=FileMetadata()),
        webcams=AsyncMock(return_value=[]),
    ):
        await _setup(hass)
    attrs = hass.states.get("sensor.centauri_printer_status").attributes
    assert attrs["lights"] == []
    assert attrs["filaments"] == []
    assert attrs["babystep"] is None
    assert hass.states.get("sensor.centauri_filaments") is None
    assert not hass.states.async_entity_ids("light")
