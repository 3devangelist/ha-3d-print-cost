"""Safe power, pause / resume / cancel, and the print history used by the panel."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.fdm_printer_manager.adapters.moonraker import (
    KlipperStatus,
    MoonrakerConnectionError,
)
from custom_components.fdm_printer_manager.const import DOMAIN

from .helpers import BAMBU_SENSORS, SERIAL, register_bambu_printer

CLIENT = "custom_components.fdm_printer_manager.coordinator.MoonrakerClient"
PLUG = "switch.escritorio_tomada_ender6"
ENERGY = "sensor.escritorio_tomada_ender6_energy"


class FakePrinter:
    def __init__(self) -> None:
        self.reading: KlipperStatus | Exception = KlipperStatus(state="standby")
        self.calls: list[str] = []

    async def status(self) -> KlipperStatus:
        if isinstance(self.reading, Exception):
            raise self.reading
        return self.reading

    def recorder(self, name: str):
        async def _call() -> None:
            self.calls.append(name)

        return _call


@pytest.fixture
def printer():
    fake = FakePrinter()

    async def host_is_up(host: str, port: int) -> bool:  # the computer answers while Moonraker does
        return not isinstance(fake.reading, Exception)

    with (
        patch.multiple(
            CLIENT,
            status=AsyncMock(side_effect=fake.status),
            metadata=AsyncMock(side_effect=MoonrakerConnectionError("none")),
            pause=AsyncMock(side_effect=fake.recorder("pause")),
            resume=AsyncMock(side_effect=fake.recorder("resume")),
            cancel=AsyncMock(side_effect=fake.recorder("cancel")),
            shutdown_host=AsyncMock(side_effect=fake.recorder("shutdown")),
            webcams=AsyncMock(return_value=[]),
            hostname=AsyncMock(return_value="Ender6"),
        ),
        patch("custom_components.fdm_printer_manager.coordinator.host_is_up", AsyncMock(side_effect=host_is_up)),
    ):
        yield fake


async def setup(hass: HomeAssistant) -> MockConfigEntry:
    hass.states.async_set(PLUG, "on")
    hass.states.async_set(ENERGY, "10000", {"unit_of_measurement": "Wh"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Ender 6",
        unique_id="klipper_192.168.1.89_7125",
        data={
            "printer_type": "klipper",
            "printer_name": "Ender 6",
            "host": "192.168.1.89",
            "port": 7125,
            "switch_entity": PLUG,
            "energy_entity": ENERGY,
            "price_per_kwh": 0.16,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_safe_power_refuses_while_printing(hass: HomeAssistant, freezer, printer) -> None:
    printer.reading = KlipperStatus(state="printing", filename="a.gcode", progress=10)
    await setup(hass)
    off = async_mock_service(hass, "homeassistant", "turn_off")
    assert hass.states.get("switch.ender_6_safe_power").state == "on"
    with pytest.raises(HomeAssistantError, match="printing"):
        await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.ender_6_safe_power"}, blocking=True)
    assert off == []
    assert printer.calls == []


async def test_safe_power_shuts_the_host_down_then_cuts_the_plug(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, printer
) -> None:
    freezer.move_to("2026-10-29 10:00:00+00:00")
    await setup(hass)
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.ender_6_safe_power"}, blocking=True)
    assert printer.calls == ["shutdown"]
    assert hass.states.get("switch.ender_6_safe_power").attributes["phase"] == "shutting_down"
    assert cut == []  # not yet: the computer is still shutting down

    printer.reading = MoonrakerConnectionError("host is down")
    for _ in range(6):
        await tick(hass, freezer, 5)
    assert len(cut) == 1
    assert cut[0].data["entity_id"] == PLUG
    assert hass.states.get("switch.ender_6_safe_power").attributes["phase"] in ("on", "off")


def phase(hass: HomeAssistant, entity_id: str = "switch.ender_6_safe_power") -> str:
    return hass.states.get(entity_id).attributes["phase"]


async def test_safe_power_waits_for_the_nozzle_to_cool(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, printer
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    printer.reading = KlipperStatus(state="complete", nozzle_temp=150.0)
    await setup(hass)
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.ender_6_safe_power"}, blocking=True)
    assert phase(hass) == "cooling"
    assert hass.states.get("switch.ender_6_safe_power").attributes["cool_to"] == 80
    assert printer.calls == []  # no shutdown yet: the hotend fan must keep running

    printer.reading = KlipperStatus(state="complete", nozzle_temp=95.0)
    await tick(hass, freezer, 5)
    assert phase(hass) == "cooling"
    assert printer.calls == []

    printer.reading = KlipperStatus(state="complete", nozzle_temp=79.0)
    await tick(hass, freezer, 5)
    assert printer.calls == ["shutdown"]
    assert phase(hass) == "shutting_down"
    assert cut == []

    printer.reading = MoonrakerConnectionError("host is down")
    for _ in range(6):
        await tick(hass, freezer, 5)
    assert len(cut) == 1


async def test_safe_power_gives_up_when_the_nozzle_stays_hot(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, printer, caplog: pytest.LogCaptureFixture
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    printer.reading = KlipperStatus(state="standby", nozzle_temp=210.0)
    await setup(hass)
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.ender_6_safe_power"}, blocking=True)
    assert phase(hass) == "cooling"

    await tick(hass, freezer, 30 * 60 + 5)
    assert cut == []
    assert printer.calls == []
    assert phase(hass) == "on"
    assert "nozzle still at 210 °C after 30 min" in caplog.text


async def test_safe_power_cuts_at_once_without_a_temperature_reading(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, printer
) -> None:
    printer.reading = MoonrakerConnectionError("printer is off")
    await setup(hass)
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.ender_6_safe_power"}, blocking=True)
    assert len(cut) == 1
    assert printer.calls == []


async def test_turning_on_cancels_the_cooling(hass: HomeAssistant, freezer: FrozenDateTimeFactory, printer) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    printer.reading = KlipperStatus(state="complete", nozzle_temp=150.0)
    await setup(hass)
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")
    async_mock_service(hass, "homeassistant", "turn_on")
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.ender_6_safe_power"}, blocking=True)
    assert phase(hass) == "cooling"

    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.ender_6_safe_power"}, blocking=True)
    await hass.async_block_till_done()
    assert phase(hass) == "on"

    printer.reading = KlipperStatus(state="complete", nozzle_temp=40.0)
    for _ in range(3):
        await tick(hass, freezer, 5)
    assert cut == []
    assert printer.calls == []


async def test_a_new_print_cancels_the_cooling(hass: HomeAssistant, freezer: FrozenDateTimeFactory, printer) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    printer.reading = KlipperStatus(state="complete", nozzle_temp=150.0)
    await setup(hass)
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.ender_6_safe_power"}, blocking=True)
    printer.reading = KlipperStatus(state="printing", filename="b.gcode", progress=1, nozzle_temp=150.0)
    for _ in range(3):
        await tick(hass, freezer, 5)
    assert cut == []
    assert printer.calls == []
    assert phase(hass) == "on"


async def test_bambu_safe_power_waits_for_the_nozzle_to_cool(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    device_id = register_bambu_printer(hass)
    nozzle = "sensor.x2d_nozzle_temperature"
    er.async_get(hass).async_get_or_create(
        "sensor",
        "bambu_lab",
        f"{SERIAL}_nozzle_temp",
        suggested_object_id="x2d_nozzle_temperature",
        device_id=device_id,
        translation_key="nozzle_temp",
    )
    plug = "switch.x2d_plug"
    hass.states.async_set(plug, "on")
    hass.states.async_set(BAMBU_SENSORS["print_status"], "finish")
    hass.states.async_set(nozzle, "120", {"unit_of_measurement": "°C"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="X2D",
        unique_id=device_id,
        data={
            "printer_device_id": device_id,
            "printer_name": "X2D",
            "printer_serial": SERIAL,
            "print_status_entity": BAMBU_SENSORS["print_status"],
            "switch_entity": plug,
            "price_per_kwh": 0.16,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")

    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.x2d_safe_power"}, blocking=True)
    assert phase(hass, "switch.x2d_safe_power") == "cooling"
    assert cut == []

    hass.states.async_set(nozzle, "70", {"unit_of_measurement": "°C"})
    await tick(hass, freezer, 5)
    assert len(cut) == 1
    assert cut[0].data["entity_id"] == plug


async def test_pause_resume_cancel_buttons(hass: HomeAssistant, printer) -> None:
    printer.reading = KlipperStatus(state="printing", filename="a.gcode", progress=10)
    await setup(hass)
    assert hass.states.get("button.ender_6_pause_print").state != "unavailable"
    assert hass.states.get("button.ender_6_resume_print").state == "unavailable"
    await hass.services.async_call("button", "press", {"entity_id": "button.ender_6_pause_print"}, blocking=True)
    assert printer.calls == ["pause"]

    printer.reading = KlipperStatus(state="paused", filename="a.gcode", progress=10)
    await hass.config_entries.async_entries(DOMAIN)[0].runtime_data._async_poll_klipper(None)
    await hass.async_block_till_done()
    assert hass.states.get("button.ender_6_resume_print").state != "unavailable"
    assert hass.states.get("button.ender_6_pause_print").state == "unavailable"
    await hass.services.async_call("button", "press", {"entity_id": "button.ender_6_resume_print"}, blocking=True)
    await hass.services.async_call("button", "press", {"entity_id": "button.ender_6_cancel_print"}, blocking=True)
    assert printer.calls == ["pause", "resume", "cancel"]


async def test_history_websocket(hass: HomeAssistant, hass_ws_client, freezer, printer) -> None:
    # No fixed date here: the websocket access token is checked against the real clock.
    entry = await setup(hass)
    coordinator = entry.runtime_data
    for name, state in (("a.gcode", "complete"), ("b.gcode", "cancelled")):
        printer.reading = KlipperStatus(state="printing", filename=name, progress=1)
        await tick(hass, freezer, 5)
        printer.reading = KlipperStatus(state=state, filename=name, progress=100)
        await tick(hass, freezer, 600)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert [h["name"] for h in coordinator.state.history] == ["b.gcode", "a.gcode"]

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": f"{DOMAIN}/history", "limit": 1})
    msg = await client.receive_json()
    assert msg["success"]
    assert msg["result"]["total"] == 2
    row = msg["result"]["rows"][0]
    assert row["name"] == "b.gcode"
    assert row["result"] == "cancelled"
    assert row["printer"] == "Ender 6"
    assert row["entry_id"] == entry.entry_id
