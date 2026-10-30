"""Safe power, pause / resume / cancel, and the print history used by the panel."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
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
    with patch.multiple(
        CLIENT,
        status=AsyncMock(side_effect=fake.status),
        metadata=AsyncMock(side_effect=MoonrakerConnectionError("none")),
        pause=AsyncMock(side_effect=fake.recorder("pause")),
        resume=AsyncMock(side_effect=fake.recorder("resume")),
        cancel=AsyncMock(side_effect=fake.recorder("cancel")),
        shutdown_host=AsyncMock(side_effect=fake.recorder("shutdown")),
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
