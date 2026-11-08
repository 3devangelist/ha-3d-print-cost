"""0.14.0: the filament really used by every print (also cancelled and failed ones)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import Event, HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.fdm_printer_manager.adapters.moonraker import (
    FileMetadata,
    KlipperStatus,
    history_filament,
    parse_metadata,
    parse_status,
)
from custom_components.fdm_printer_manager.const import DOMAIN, EVENT_PRINT_FINISHED
from custom_components.fdm_printer_manager.models import (
    ElectricityAccumulator,
    ElectricitySample,
    PrintJob,
    PrintResult,
    filament_price,
)
from custom_components.fdm_printer_manager.sheets import COLUMNS, sheet_row
from custom_components.fdm_printer_manager.tracker import PrinterSnapshot, PrintTracker, TransitionKind

T0 = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)
PRICE = Decimal("0.20")
# Real file of the Ender 6 (Moonraker metadata): ASA, 280.68 g for 112204.07 mm.
VESPA = "Vespa_tampao_V0_v1 (1)_ASA_14h37m.gcode"
VESPA_META = {"filament_total": 112204.07, "filament_weight_total": 280.68, "filament_type": "ASA"}
VESPA_G_PER_MM = 280.68 / 112204.07


def _job() -> PrintJob:
    return PrintJob(printer_id="p", started_at=T0, electricity=ElectricityAccumulator())


def snap(minute: float, status: str | None, *, job: str | None = VESPA, mm: float | None = None, **kw):
    ts = T0 + timedelta(minutes=minute)
    return PrinterSnapshot(
        timestamp=ts,
        status=status,
        job_name=job,
        electricity=ElectricitySample(ts, power_w=200.0, energy_kwh=None, price_per_kwh=PRICE),
        filament_mm=mm,
        **kw,
    )


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
def test_largest_length_seen_is_kept() -> None:
    job = _job()
    assert job.filament_mm is None  # unknown is never zero
    for value in (0.0, 120.5, 9343.1, None, -5, float("nan"), "x"):
        job.record_filament(value)
    assert job.filament_mm == pytest.approx(9343.1)
    job.record_filament(0.0)  # a restarted Klipper reads 0: nothing is lost
    assert job.filament_mm == pytest.approx(9343.1)


def test_counter_started_again_in_the_same_print_adds_up() -> None:
    """Klipper restarted and the same file was started again: both attempts used filament."""
    job = _job()
    job.record_filament(5000.0)
    job.record_filament(0.0)
    job.record_filament(40.0)  # the new attempt moves: 5000 mm are kept
    job.record_filament(1000.0)
    assert job.filament_mm == pytest.approx(6000.0)


def test_final_value_only_raises() -> None:
    job = _job()
    job.record_filament(9343.1)
    job.record_final_filament(0.0)  # Moonraker's history after a Klipper restart
    assert job.filament_mm == pytest.approx(9343.1)
    job.record_final_filament(9400.0)  # the last seconds HA did not poll
    assert job.filament_mm == pytest.approx(9400.0)


def test_grams_and_cost_are_from_real_values_only() -> None:
    job = _job()
    job.record_filament(10000.0)
    assert job.filament_g is None  # no weight from the slicer: unknown
    assert job.filament_cost is None
    job.set_filament_file(VESPA_G_PER_MM, "ASA")
    assert job.filament_g == pytest.approx(25.015, abs=0.001)  # 10 m of ASA at 2.5015 g/m
    assert job.filament_cost is None  # no price
    job.filament_price_per_kg = Decimal("25")
    assert isinstance(job.filament_cost, Decimal)
    assert float(job.filament_cost) == pytest.approx(0.6254, abs=0.0001)
    summary = job.summary()
    assert summary["filament_mm"] == 10000.0
    assert summary["filament_g"] == pytest.approx(25.02)
    assert summary["filament_type"] == "ASA"
    assert summary["filament_cost"] == pytest.approx(0.6254)


def test_filament_survives_a_restart() -> None:
    job = _job()
    job.record_filament(3000.0)
    job.record_filament(10.0)
    job.record_filament(500.0)
    job.set_filament_file(VESPA_G_PER_MM, "ASA")
    job.filament_price_per_kg = Decimal("24.90")
    back = PrintJob.from_dict(job.as_dict())
    assert back.filament_mm == pytest.approx(3500.0)
    assert back.filament_type == "ASA"
    assert back.filament_g == pytest.approx(job.filament_g)
    assert back.filament_cost == job.filament_cost
    # A job saved before 0.14.0 has no filament: unknown, not 0.
    old = job.as_dict()
    for key in [k for k in old if k.startswith("filament")]:
        del old[key]
    assert PrintJob.from_dict(old).filament_mm is None


def test_filament_price_option() -> None:
    assert filament_price(None) is None
    assert filament_price("") is None
    assert filament_price(-1) is None
    assert filament_price(24.9) == Decimal("24.9")


# --------------------------------------------------------------------------- #
# Tracker
# --------------------------------------------------------------------------- #
def test_klipper_restart_mid_print_keeps_the_largest_length() -> None:
    """Real case (Ender 6, 08-11): klippy_disconnect, Moonraker's history says 0 mm."""
    tracker = PrintTracker("p")
    tracker.process(snap(0, "printing", mm=0.0, filament_g_per_mm=VESPA_G_PER_MM, filament_type="ASA"))
    tracker.process(snap(30, "printing", mm=4100.0))
    tracker.process(snap(60, "printing", mm=9343.1))
    tracker.process(snap(61, "standby", mm=0.0))  # Klipper restarting: counter back to 0, job kept open
    transitions = tracker.process(snap(62, "error", mm=None))  # Klipper shutdown
    assert [t.kind for t in transitions] == [TransitionKind.ENDED]
    job = transitions[0].job
    assert job.result is PrintResult.FAILED
    job.record_final_filament(history_filament([], VESPA, T0.timestamp()))
    assert job.filament_mm == pytest.approx(9343.1)
    assert job.filament_g == pytest.approx(9343.1 * VESPA_G_PER_MM)


def test_cancelled_print_keeps_its_filament() -> None:
    tracker = PrintTracker("p")
    tracker.process(snap(0, "printing", mm=0.0, filament_g_per_mm=VESPA_G_PER_MM, filament_type="ASA"))
    tracker.process(snap(10, "printing", mm=1500.0))
    transitions = tracker.process(snap(11, "cancelled", mm=1526.1))
    job = transitions[0].job
    assert job.result is PrintResult.CANCELLED
    assert job.filament_mm == pytest.approx(1526.1)
    assert job.summary()["filament_g"] == pytest.approx(round(1526.1 * VESPA_G_PER_MM, 2))


def test_cancelled_while_heating_used_nothing() -> None:
    tracker = PrintTracker("p")
    tracker.process(snap(0, "printing", mm=0.0))
    job = tracker.process(snap(3, "cancelled", mm=0.0))[0].job
    assert job.filament_mm == 0.0  # seen by the printer: a real 0


def test_another_print_does_not_take_the_filament_of_the_first() -> None:
    tracker = PrintTracker("p")
    tracker.process(snap(0, "printing", job="a.gcode", mm=3000.0, filament_g_per_mm=0.003, filament_type="PLA"))
    transitions = tracker.process(
        snap(5, "printing", job="b.gcode", mm=20.0, filament_g_per_mm=0.0025, filament_type="ASA")
    )
    ended, started = transitions[0].job, transitions[1].job
    assert ended.filament_mm == pytest.approx(3000.0)
    assert ended.filament_type == "PLA"
    assert ended.filament_g_per_mm == pytest.approx(0.003)
    assert started.filament_mm == pytest.approx(20.0)
    assert started.filament_type == "ASA"


def test_bambu_snapshot_leaves_filament_unknown() -> None:
    tracker = PrintTracker("p")
    tracker.process(snap(0, "running", job="plate_1"))
    job = tracker.process(snap(30, "finish", job="plate_1"))[0].job
    assert job.filament_mm is None
    assert job.summary()["filament_mm"] is None


# --------------------------------------------------------------------------- #
# Moonraker parsing
# --------------------------------------------------------------------------- #
def test_parse_status_reads_filament_used() -> None:
    status = {"print_stats": {"state": "printing", "filename": VESPA, "filament_used": 9343.1112999998}}
    assert parse_status(status, "ready").filament_mm == pytest.approx(9343.1113)
    assert parse_status({}, "shutdown").filament_mm is None


def test_parse_metadata_reads_the_file_filament() -> None:
    meta = parse_metadata({"estimated_time": 52639, **VESPA_META})
    assert meta.filament_type == "ASA"
    assert meta.filament_g_per_mm * 1000 == pytest.approx(2.5015, abs=0.0001)  # g per metre
    assert parse_metadata({"filament_total": 1000}).filament_g_per_mm is None  # no weight: unknown
    assert parse_metadata({"filament_weight_total": 3, "filament_total": 0}).filament_g_per_mm is None


def test_history_filament_picks_the_last_attempt_of_the_print() -> None:
    start = T0.timestamp()
    jobs = [
        {"filename": VESPA, "start_time": start + 3600, "filament_used": 800.0},  # last attempt
        {"filename": VESPA, "start_time": start + 60, "filament_used": 526.1},
        {"filename": "other.gcode", "start_time": start + 4000, "filament_used": 99.0},
        {"filename": VESPA, "start_time": start - 86400, "filament_used": 5000.0},  # yesterday
    ]
    assert history_filament(jobs, VESPA, start, start + 7200) == 800.0
    assert history_filament(jobs, VESPA, start, start + 1800) == 526.1  # the later one began after the end
    assert history_filament(jobs, "missing.gcode", start) is None
    assert history_filament(None, VESPA, start) is None
    assert history_filament("junk", VESPA, start) is None


def test_history_zero_is_not_a_measurement() -> None:
    """klippy_shutdown / klippy_disconnect / interrupted: Moonraker writes 0 mm."""
    start = T0.timestamp()
    jobs = [{"filename": VESPA, "start_time": start + 30, "filament_used": 0.0, "status": "klippy_disconnect"}]
    assert history_filament(jobs, VESPA, start) is None


# --------------------------------------------------------------------------- #
# Google Sheets
# --------------------------------------------------------------------------- #
def test_new_sheet_columns_are_at_the_end() -> None:
    assert COLUMNS[:12] == (
        "printer",
        "file",
        "result",
        "result_by",
        "started",
        "finished",
        "duration_min",
        "energy_kwh",
        "electricity_cost",
        "currency",
        "notes",
        "job_id",
    )
    assert COLUMNS[12:] == ("filament_mm", "filament_g", "filament_type", "filament_cost")
    row = sheet_row(
        {"filament_mm": 9343.1, "filament_g": 23.37, "filament_type": "ASA", "filament_cost": 0.5843},
        ZoneInfo("Europe/Lisbon"),
    )
    assert tuple(row) == COLUMNS
    assert (row["filament_mm"], row["filament_g"], row["filament_type"], row["filament_cost"]) == (
        9343.1,
        23.37,
        "ASA",
        0.5843,
    )


# --------------------------------------------------------------------------- #
# End to end (Moonraker mocked)
# --------------------------------------------------------------------------- #
CLIENT = "custom_components.fdm_printer_manager.coordinator.MoonrakerClient"
ENERGY = "sensor.escritorio_tomada_ender6_energy"


class FakeMoonraker:
    def __init__(self) -> None:
        self.reading = KlipperStatus(state="standby")
        self.jobs: list[dict] = []

    async def status(self) -> KlipperStatus:
        return self.reading

    async def history(self, limit: int = 10) -> list[dict]:
        return self.jobs


@pytest.fixture
def moonraker():
    fake = FakeMoonraker()
    with patch.multiple(
        CLIENT,
        status=AsyncMock(side_effect=fake.status),
        history=AsyncMock(side_effect=fake.history),
        metadata=AsyncMock(return_value=parse_metadata({"estimated_time": 52639, **VESPA_META})),
        webcams=AsyncMock(return_value=[]),
    ):
        yield fake


async def _setup(hass: HomeAssistant, **options) -> MockConfigEntry:
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
            "switch_entity": "switch.escritorio_tomada_ender6",
            "price_per_kwh": 0.2,
            "integration_method": "left",
        },
        options=options,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: float) -> None:
    freezer.tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def _events(hass: HomeAssistant) -> list[Event]:
    captured: list[Event] = []
    hass.bus.async_listen(EVENT_PRINT_FINISHED, captured.append)
    return captured


async def test_klipper_shutdown_mid_print_books_the_filament_seen(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, moonraker
) -> None:
    freezer.move_to("2026-11-08 10:00:00+00:00")
    events = _events(hass)
    entry = await _setup(hass, filament_price_per_kg=25)
    start = datetime.fromisoformat("2026-11-08 10:00:00+00:00").timestamp()

    moonraker.reading = KlipperStatus(state="printing", filename=VESPA, filament_mm=0.0)
    await _poll(hass, freezer, 0.1)
    moonraker.reading = KlipperStatus(state="printing", filename=VESPA, filament_mm=9343.1)
    await _poll(hass, freezer, 60)
    moonraker.jobs = [{"filename": VESPA, "start_time": start, "filament_used": 0.0, "status": "klippy_shutdown"}]
    moonraker.reading = parse_status({}, "shutdown")  # Klipper shut down: no print_stats
    await _poll(hass, freezer, 1)

    assert len(events) == 1
    data = events[0].data
    assert data["result"] == "failed"
    assert data["filament_mm"] == pytest.approx(9343.1)
    assert data["filament_g"] == pytest.approx(round(9343.1 * VESPA_G_PER_MM, 2))
    assert data["filament_type"] == "ASA"
    assert data["filament_cost"] == pytest.approx(round(9343.1 * VESPA_G_PER_MM * 25 / 1000, 4))
    assert any("Moonraker's history has no length" in note for note in data["notes"])
    # Kept in the history (panel) as well.
    history = entry.runtime_data.state.history
    assert history[0]["filament_mm"] == pytest.approx(9343.1)


async def test_cancelled_print_takes_the_final_length_from_the_history(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, moonraker
) -> None:
    freezer.move_to("2026-11-08 10:00:00+00:00")
    events = _events(hass)
    await _setup(hass)  # no filament price: cost stays empty
    start = datetime.fromisoformat("2026-11-08 10:00:00+00:00").timestamp()

    moonraker.reading = KlipperStatus(state="printing", filename=VESPA, filament_mm=10.0)
    await _poll(hass, freezer, 0.1)
    moonraker.reading = KlipperStatus(state="printing", filename=VESPA, filament_mm=500.0)
    await _poll(hass, freezer, 10)
    moonraker.jobs = [{"filename": VESPA, "start_time": start, "filament_used": 526.1473, "status": "cancelled"}]
    moonraker.reading = KlipperStatus(state="cancelled", filename=VESPA, filament_mm=None)
    await _poll(hass, freezer, 1)

    data = events[0].data
    assert data["result"] == "cancelled"
    assert data["filament_mm"] == pytest.approx(526.1)
    assert data["filament_g"] == pytest.approx(round(526.1473 * VESPA_G_PER_MM, 2))
    assert data["filament_cost"] is None
    assert not any("Filament" in note for note in data["notes"])


async def test_unknown_filament_gets_a_note(hass: HomeAssistant, freezer: FrozenDateTimeFactory, moonraker) -> None:
    freezer.move_to("2026-11-08 10:00:00+00:00")
    events = _events(hass)
    await _setup(hass, filament_price_per_kg=25)
    moonraker.reading = KlipperStatus(state="printing", filename=VESPA)  # an old Moonraker: no filament_used
    await _poll(hass, freezer, 0.1)
    moonraker.reading = KlipperStatus(state="complete", filename=VESPA)
    await _poll(hass, freezer, 30)
    data = events[0].data
    assert data["filament_mm"] is None
    assert data["filament_g"] is None
    assert data["filament_cost"] is None
    assert "Filament used unknown: the printer did not report it" in data["notes"]


def test_metadata_without_filament_fields() -> None:
    """FileMetadata keeps working without filament fields (older callers)."""
    assert FileMetadata(estimated_time_s=10).filament_g_per_mm is None
