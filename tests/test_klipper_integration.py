"""End-to-end: a Klipper print read directly from Moonraker (client mocked)."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.printer_manager.adapters.moonraker import (
    FileMetadata,
    KlipperStatus,
    MoonrakerConnectionError,
)
from custom_components.printer_manager.const import DOMAIN, EVENT_PRINT_FINISHED

CLIENT = "custom_components.printer_manager.coordinator.MoonrakerClient"
ENERGY = "sensor.escritorio_tomada_ender6_energy"
PRICE = "input_number.preco_do_kwh"
FILE = "gsu_cover_PLA_1h12m.gcode"


class FakePrinter:
    """What Moonraker answers; tests change ``reading`` between polls."""

    def __init__(self) -> None:
        self.reading: KlipperStatus | Exception = KlipperStatus(state="standby")

    async def status(self) -> KlipperStatus:
        if isinstance(self.reading, Exception):
            raise self.reading
        return self.reading


@pytest.fixture
def printer():
    fake = FakePrinter()
    with patch.multiple(
        CLIENT,
        status=AsyncMock(side_effect=fake.status),
        metadata=AsyncMock(return_value=FileMetadata(estimated_time_s=4320, filament_weight_g=31.7)),
    ):
        yield fake


@pytest.fixture
def events(hass: HomeAssistant) -> list[Event]:
    captured: list[Event] = []
    hass.bus.async_listen(EVENT_PRINT_FINISHED, captured.append)
    return captured


async def poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: float, energy_wh: float) -> None:
    hass.states.async_set(ENERGY, str(energy_wh), {"unit_of_measurement": "Wh"})
    freezer.tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def setup_ender(hass: HomeAssistant, **extra) -> MockConfigEntry:
    hass.states.async_set(PRICE, "0.16", {"unit_of_measurement": "EUR/kWh"})
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
            "web_url": "http://192.168.1.89/",
            "switch_entity": "switch.escritorio_tomada_ender6",
            "energy_entity": ENERGY,
            "price_entity": PRICE,
            "integration_method": "left",
            "filament_fallback": "none",
            **extra,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_klipper_print_end_to_end(hass: HomeAssistant, freezer: FrozenDateTimeFactory, printer, events) -> None:
    freezer.move_to("2026-10-28 10:00:00+00:00")
    entry = await setup_ender(hass)

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert device.name == "Ender 6"
    assert device.manufacturer == "Klipper"
    assert device.configuration_url == "http://192.168.1.89/"
    assert hass.states.get("sensor.ender_6_current_print_phase").state == "idle"

    printer.reading = KlipperStatus(state="printing", filename=FILE, progress=0.0, print_duration_s=0)
    await poll(hass, freezer, 0.1, 10000)
    assert hass.states.get("sensor.ender_6_current_print_phase").state == "active"

    printer.reading = KlipperStatus(state="printing", filename=FILE, progress=50.0, print_duration_s=2160)
    await poll(hass, freezer, 36, 10500)
    printer.reading = KlipperStatus(state="complete", filename=FILE, progress=100.0, print_duration_s=4320)
    await poll(hass, freezer, 36, 11000)

    assert len(events) == 1
    data = events[0].data
    assert data["printer"] == "Ender 6"
    assert data["result"] == "success"
    assert data["name"] == FILE
    assert float(data["energy_kwh"]) == pytest.approx(1.0)
    assert hass.states.get("sensor.ender_6_last_print_result").state == "success"


async def test_unreachable_printer_keeps_the_print_open(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, printer, events
) -> None:
    freezer.move_to("2026-10-28 10:00:00+00:00")
    await setup_ender(hass)
    printer.reading = KlipperStatus(state="printing", filename=FILE, progress=10.0, print_duration_s=600)
    await poll(hass, freezer, 0.1, 10000)

    printer.reading = MoonrakerConnectionError("timeout")  # printer lost power
    await poll(hass, freezer, 5, 10100)
    assert hass.states.get("sensor.ender_6_current_print_phase").state == "active"
    assert events == []

    printer.reading = KlipperStatus(state="error", filename=FILE, progress=10.0, print_duration_s=600)
    await poll(hass, freezer, 5, 10100)
    assert len(events) == 1
    assert events[0].data["result"] == "failed"
