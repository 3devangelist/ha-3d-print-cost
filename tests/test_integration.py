"""End-to-end: a print seen through Home Assistant states."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.image import Image
from homeassistant.core import Event, HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.printer_manager.adapters.haspelsync import parse_print_usage
from custom_components.printer_manager.const import (
    CONF_COVER_IMAGE_ENTITY,
    CONF_ENERGY_ENTITY,
    CONF_FILAMENT_FALLBACK,
    CONF_HASPELSYNC_PRINTER_ID,
    CONF_HASPELSYNC_URL,
    CONF_INTEGRATION_METHOD,
    CONF_JOB_NAME_ENTITY,
    CONF_POWER_ENTITY,
    CONF_PRICE_ENTITY,
    CONF_PRINT_STATUS_ENTITY,
    CONF_PRINT_WEIGHT_ENTITY,
    CONF_PRINTER_DEVICE_ID,
    CONF_PRINTER_NAME,
    CONF_PRINTER_SERIAL,
    CONF_PROGRESS_ENTITY,
    CONF_SPOOLMAN_URL,
    DOMAIN,
    EVENT_PRINT_FINISHED,
)
from custom_components.printer_manager.diagnostics import async_get_config_entry_diagnostics
from custom_components.printer_manager.models import SpoolInfo

from .helpers import BAMBU_SENSORS, SERIAL, register_bambu_printer

STATUS = BAMBU_SENSORS["print_status"]
JOB = BAMBU_SENSORS["subtask_name"]
POWER = "sensor.x2d_power"
ENERGY = "sensor.x2d_energy"
PRICE = "sensor.prix_electricite_octopus"


def make_entry(hass: HomeAssistant, **extra) -> MockConfigEntry:
    device_id = register_bambu_printer(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="X2D",
        unique_id=device_id,
        data={
            CONF_PRINTER_DEVICE_ID: device_id,
            CONF_PRINTER_NAME: "X2D",
            CONF_PRINTER_SERIAL: SERIAL,
            CONF_PRINT_STATUS_ENTITY: STATUS,
            CONF_JOB_NAME_ENTITY: JOB,
            CONF_PROGRESS_ENTITY: BAMBU_SENSORS["print_progress"],
            CONF_PRINT_WEIGHT_ENTITY: BAMBU_SENSORS["print_weight"],
            CONF_POWER_ENTITY: POWER,
            CONF_ENERGY_ENTITY: ENERGY,
            CONF_PRICE_ENTITY: PRICE,
            CONF_INTEGRATION_METHOD: "left",
            CONF_FILAMENT_FALLBACK: "planned_success",
            **extra,
        },
    )
    entry.add_to_hass(hass)
    return entry


def set_states(hass, *, status, energy, price="0.1876", power="300", job="Flower Lamp"):
    # Each state change is processed immediately at the same frozen instant:
    # the meter must report before the status change that ends the interval
    # (as a real plug does), otherwise its delta would land in a 0 s interval.
    hass.states.async_set(JOB, job)
    hass.states.async_set(POWER, power, {"unit_of_measurement": "W"})
    hass.states.async_set(ENERGY, energy, {"unit_of_measurement": "Wh"})
    hass.states.async_set(PRICE, price, {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(STATUS, status)


async def step(hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: float, **states) -> None:
    freezer.tick(timedelta(minutes=minutes))
    set_states(hass, **states)
    # Finalisation runs as a background task, which a plain block_till_done
    # does not wait for (newer HA happens to finish it in time, older does not).
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.fixture
def events(hass: HomeAssistant) -> list[Event]:
    captured: list[Event] = []
    hass.bus.async_listen(EVENT_PRINT_FINISHED, captured.append)
    return captured


async def test_print_with_tariff_change(hass: HomeAssistant, freezer: FrozenDateTimeFactory, events) -> None:
    freezer.move_to("2026-10-23 14:00:00+00:00")
    set_states(hass, status="idle", energy="100000")
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.x2d_current_print_phase").state == "idle"
    assert hass.states.get("sensor.x2d_current_print_total_cost").state == "unknown"

    # 14:00 print starts (meter in Wh: 100 000 Wh = 100 kWh)
    await step(hass, freezer, 0, status="prepare", energy="100000")
    assert hass.states.get("sensor.x2d_current_print_phase").state == "active"
    # 14:06 peak -> off-peak
    await step(hass, freezer, 6, status="running", energy="100060", price="0.1405")
    # 15:00 end
    await step(hass, freezer, 54, status="running", energy="100600", price="0.1405")
    current = hass.states.get("sensor.x2d_current_print_energy")
    assert float(current.state) == pytest.approx(0.6)

    await step(hass, freezer, 0, status="finish", energy="100600", price="0.1405")
    await hass.async_block_till_done()

    expected = Decimal("0.06") * Decimal("0.1876") + Decimal("0.54") * Decimal("0.1405")
    last_cost = hass.states.get("sensor.x2d_last_print_electricity_cost")
    assert float(last_cost.state) == pytest.approx(float(expected), abs=1e-6)
    assert last_cost.attributes["unit_of_measurement"] == "EUR"
    # electricity only -> total == electricity (filament tracking disabled)
    total = hass.states.get("sensor.x2d_last_print_total_cost")
    assert float(total.state) == pytest.approx(float(expected), abs=1e-6)
    assert total.attributes["filament_status"] == "disabled"
    assert hass.states.get("sensor.x2d_last_print_result").state == "success"
    assert float(hass.states.get("sensor.x2d_last_print_duration").state) == pytest.approx(1.0)  # hours

    assert len(events) == 1
    assert events[0].data["name"] == "Flower Lamp"
    assert events[0].data["result"] == "success"
    assert events[0].data["energy_kwh"] == pytest.approx(0.6)
    event_state = hass.states.get("event.x2d_print_finished")
    assert event_state.attributes["event_type"] == "print_finished"
    assert event_state.attributes["total_cost"] == pytest.approx(float(expected), abs=1e-4)

    # Finish state persists on the printer: no second job / event
    await step(hass, freezer, 5, status="finish", energy="100605")
    assert len(events) == 1


async def test_restart_during_print(hass: HomeAssistant, freezer: FrozenDateTimeFactory, events) -> None:
    freezer.move_to("2026-10-23 14:00:00+00:00")
    set_states(hass, status="idle", energy="100000")
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await step(hass, freezer, 0, status="running", energy="100000")
    await step(hass, freezer, 30, status="running", energy="100150")
    job_id = entry.runtime_data.tracker.active_job.job_id

    # --- reload == restart: state flushed to storage and restored ---
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=20))
    set_states(hass, status="running", energy="100250")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    active = entry.runtime_data.tracker.active_job
    assert active is not None
    assert active.job_id == job_id  # same job, not a new one
    assert active.energy_kwh == pytest.approx(0.25)

    await step(hass, freezer, 10, status="finish", energy="100300")
    assert len(events) == 1
    assert events[0].data["energy_kwh"] == pytest.approx(0.3)
    assert events[0].data["job_id"] == job_id
    # Last print survives a restart too
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.x2d_last_print_energy").state) == pytest.approx(0.3)


async def test_show_last_print_switch(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to("2026-10-23 14:00:00+00:00")
    set_states(hass, status="idle", energy="100000")
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    switch = "switch.x2d_show_last_print"
    assert hass.states.get(switch).state == "off"

    await step(hass, freezer, 0, status="running", energy="100000")
    await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
    await step(hass, freezer, 10, status="running", energy="100100")
    assert hass.states.get(switch).state == "on"  # stays on during the same print

    await step(hass, freezer, 0, status="finish", energy="100100")
    await hass.async_block_till_done()
    assert hass.states.get(switch).state == "on"

    # A new print brings the card back to the current print
    await step(hass, freezer, 5, status="running", energy="100100")
    assert hass.states.get(switch).state == "off"


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


async def test_last_print_keeps_its_thumbnail(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, events, tmp_path
) -> None:
    hass.config.config_dir = str(tmp_path)  # the thumbnail is a real file
    freezer.move_to("2026-10-23 14:00:00+00:00")
    cover = "image.x2d_cover_image"
    hass.states.async_set(cover, "2026-10-23T14:00:00+00:00", {"entity_picture": "/api/image_proxy/cover?token=a"})
    set_states(hass, status="idle", energy="100000")
    entry = make_entry(hass, **{CONF_COVER_IMAGE_ENTITY: cover})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("image.x2d_last_print_image").state == "unavailable"

    await step(hass, freezer, 0, status="running", energy="100000")
    current = hass.states.get("sensor.x2d_current_print_total_cost")
    assert current.attributes["entity_picture"] == "/api/image_proxy/cover?token=a"

    get_image = AsyncMock(return_value=Image("image/png", PNG))
    with patch("custom_components.printer_manager.coordinator.async_get_image", get_image):
        await step(hass, freezer, 30, status="finish", energy="100150")
        await hass.async_block_till_done(wait_background_tasks=True)  # finalisation writes the file
    get_image.assert_awaited_once_with(hass, cover)
    assert len(events) == 1

    image = hass.states.get("image.x2d_last_print_image")
    assert image.state == "2026-10-23T14:30:00+00:00"
    last = hass.states.get("sensor.x2d_last_print_total_cost")
    assert last.attributes["entity_picture"] == image.attributes["entity_picture"]
    assert "entity_picture" not in hass.states.get("sensor.x2d_current_print_total_cost").attributes

    # The copy survives a restart, whatever the printer shows next.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.last_image == PNG
    assert hass.states.get("image.x2d_last_print_image").state == "2026-10-23T14:30:00+00:00"

    await hass.config_entries.async_remove(entry.entry_id)
    assert not list((tmp_path / ".storage").glob("*.image"))


async def test_filament_cost_from_haspelsync_and_spoolman(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, events
) -> None:
    freezer.move_to("2026-10-23 14:00:00+00:00")
    set_states(hass, status="idle", energy="0")
    entry = make_entry(
        hass,
        **{
            CONF_HASPELSYNC_URL: "http://hs:4000",
            CONF_HASPELSYNC_PRINTER_ID: SERIAL,
            CONF_SPOOLMAN_URL: "http://spoolman:7912",
        },
    )
    started = dt_util.utcnow()
    summary = {
        "consumptionBooked": True,
        "lastPrintSummary": {
            "state": "FINISH",
            "jobName": "Flower Lamp",
            "endedAt": int((started + timedelta(hours=1)).timestamp() * 1000),
            "rows": [
                {"amsId": "A1", "grams": 62.4, "status": "booked", "spoolId": 23},
                {"amsId": "A2", "grams": 22.2, "status": "booked", "spoolId": 45},
            ],
        },
    }
    spools = {
        23: SpoolInfo(spool_id=23, spool_price=Decimal("19.39"), initial_weight_g=1000, material="PLA", name="Black"),
        45: SpoolInfo(spool_id=45, spool_price=Decimal("22.97"), initial_weight_g=1000, material="PLA", name="White"),
    }

    async def fake_usage(printer_id, *, job_name, started_at):
        return parse_print_usage(summary, job_name=job_name, started_at=started_at)

    with (
        patch(
            "custom_components.printer_manager.coordinator.HaspelsyncClient.get_current_slots",
            AsyncMock(return_value={}),
        ),
        patch(
            "custom_components.printer_manager.coordinator.HaspelsyncClient.get_print_usage",
            side_effect=fake_usage,
        ),
        patch(
            "custom_components.printer_manager.coordinator.SpoolmanClient.get_spool",
            side_effect=lambda spool_id: spools[spool_id],
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await step(hass, freezer, 0, status="running", energy="0")
        await step(hass, freezer, 60, status="running", energy="640")
        await step(hass, freezer, 0, status="finish", energy="640")
        await hass.async_block_till_done()

    assert len(events) == 1
    data = events[0].data
    assert data["filament_weight_g"] == pytest.approx(84.6)
    assert data["filament_cost"] == pytest.approx(1.72, abs=0.005)
    assert data["total_cost"] == pytest.approx(data["electricity_cost"] + data["filament_cost"], abs=1e-3)
    assert [f["spool_id"] for f in data["filaments"]] == [23, 45]
    assert float(hass.states.get("sensor.x2d_last_print_filament_weight").state) == pytest.approx(84.6)


async def test_diagnostics_redacts_secrets(hass: HomeAssistant) -> None:
    set_states(hass, status="idle", energy="0")
    entry = make_entry(hass, **{CONF_HASPELSYNC_URL: "http://hs:4000", "haspelsync_api_key": "super-secret"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert "super-secret" not in str(diag)
    assert diag["units"]["energy"] == "Wh"
