"""0.15.2: the cost of the filament always comes from the metres used and the material."""

import math
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.fdm_printer_manager import ws_history
from custom_components.fdm_printer_manager.adapters.moonraker import (
    HISTORY_END_TOLERANCE_S,
    KlipperStatus,
    history_match,
    parse_metadata,
)
from custom_components.fdm_printer_manager.const import DOMAIN
from custom_components.fdm_printer_manager.materials import (
    DEFAULT_DIAMETER_MM,
    DENSITIES,
    GRAMS_DENSITY,
    GRAMS_SLICER,
    MATERIALS,
    filament_weight,
    generic_prices,
    grams_from_length,
    grams_note,
    refresh_row_filament,
    set_row_length,
    set_row_material,
)
from custom_components.fdm_printer_manager.models import ElectricityAccumulator, PrintJob
from custom_components.fdm_printer_manager.sheets import (
    CORRECTION_KEY,
    IN_PLACE_ONLY_KEY,
    LENGTH_CELLS,
    SheetsLog,
)

from .test_sheets import SHEETS, UPDATE_ROWS, _google_sheets_entry, _sheets_service

T0 = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)
SECTION = math.pi * (1.75 / 2) ** 2  # mm² of a 1.75 mm filament
TABLE = generic_prices({})
# Real file of the Ender 6 (Moonraker): ASA, 280.68 g for 112204.07 mm (OrcaSlicer "Generic ASA", 1.04 g/cm³).
VESPA = "Vespa_tampao_V0_v1 (1)_ASA_14h37m.gcode"
VESPA_META = {"filament_total": 112204.07, "filament_weight_total": 280.68, "filament_type": "ASA"}
VESPA_USED = 112232.56122002032  # Moonraker's history, print of 07-11 (completed)


def _grams(mm: float, density: float, diameter: float = 1.75) -> float:
    return mm * math.pi * (diameter / 2) ** 2 * density / 1000


def _job() -> PrintJob:
    return PrintJob(printer_id="p", started_at=T0, electricity=ElectricityAccumulator())


# --------------------------------------------------------------------------- #
# Densities and weight
# --------------------------------------------------------------------------- #
def test_every_material_has_a_density() -> None:
    assert set(DENSITIES) == set(MATERIALS)
    assert all(0.8 <= d <= 1.5 for d in DENSITIES.values())
    assert DENSITIES["PLA"] == 1.24
    assert DENSITIES["ASA"] == 1.07
    assert DEFAULT_DIAMETER_MM == 1.75


def test_grams_from_length() -> None:
    # 1 m of 1.75 mm PLA weighs about 3 g.
    assert grams_from_length(1000, 1.24) == pytest.approx(2.9825, abs=1e-4)
    assert grams_from_length(1000, 1.24, 2.85) == pytest.approx(1000 * math.pi * 1.425**2 * 1.24 / 1000)


def test_slicer_weight_wins_for_the_same_material() -> None:
    weight = filament_weight(10000, "ASA", g_per_mm=0.0025, file_material="ASA")
    assert (weight.grams, weight.source) == (pytest.approx(25.0), GRAMS_SLICER)
    # Material unknown, or the slicer did not name it: its weight is still the best value.
    assert filament_weight(10000, None, g_per_mm=0.0025, file_material="ASA").source == GRAMS_SLICER
    assert filament_weight(10000, "PETG", g_per_mm=0.0025).source == GRAMS_SLICER


def test_no_slicer_weight_uses_the_density_of_the_material() -> None:
    weight = filament_weight(10000, "ASA")
    assert weight.source == GRAMS_DENSITY
    assert weight.grams == pytest.approx(10000 * SECTION * 1.07 / 1000)
    assert (weight.density, weight.diameter, weight.slicer_density) == (1.07, 1.75, False)
    assert grams_note("ASA", weight) == (
        "Filament weight from the length used and the typical density of ASA (1.07 g/cm³, 1.75 mm filament)"
    )


def test_another_material_than_the_slicer_s_is_weighed_again() -> None:
    weight = filament_weight(10000, "PETG", g_per_mm=0.0025, file_material="ASA", density=1.04)
    assert weight.source == GRAMS_DENSITY
    assert weight.grams == pytest.approx(_grams(10000, 1.27))  # PETG, not the slicer's ASA density


def test_slicer_density_and_diameter_are_used() -> None:
    weight = filament_weight(10000, "ASA", file_material="ASA", density="1.04", diameter=2.85)
    assert weight.grams == pytest.approx(_grams(10000, 1.04, 2.85))
    assert weight.slicer_density
    assert "the slicer's density (1.04 g/cm³, 2.85 mm filament)" in grams_note("ASA", weight)
    # Silly values from a file are ignored.
    weight = filament_weight(10000, "ASA", density=40, diameter=0.2)
    assert weight.grams == pytest.approx(_grams(10000, 1.07))


def test_unknown_stays_unknown() -> None:
    assert filament_weight(None, "ASA").grams is None  # no length: never estimated
    unknown = filament_weight(10000, None)  # no slicer weight, material unknown
    assert unknown.grams is None
    assert grams_note(None, unknown) == "Filament weight unknown: material unknown (choose it in Recent prints)"
    assert filament_weight(10000, "Wood fill").grams is None
    assert filament_weight(0, "PLA").grams == 0.0  # a real 0 mm stays 0 g


# --------------------------------------------------------------------------- #
# The print
# --------------------------------------------------------------------------- #
def test_print_without_slicer_weight_is_weighed_by_density() -> None:
    job = _job()
    job.record_filament(4000.0)
    job.set_filament_file(None, "Generic ASA")
    assert job.filament_g == pytest.approx(_grams(4000, 1.07))
    summary = job.summary()
    assert summary["filament_g_source"] == GRAMS_DENSITY
    assert summary["filament_file_type"] == "ASA"
    back = PrintJob.from_dict(job.as_dict())
    assert back.filament_g == pytest.approx(job.filament_g)
    assert back.filament_file_type == "ASA"


def test_print_keeps_the_slicer_density_and_diameter() -> None:
    job = _job()
    job.record_filament(1000.0)
    job.set_filament_file(None, "PLA", 1.30, 1.75)
    assert job.filament_g == pytest.approx(_grams(1000, 1.30))
    back = PrintJob.from_dict(job.as_dict())
    assert (back.filament_density, back.filament_diameter_mm) == (1.30, 1.75)


def test_metadata_reads_density_and_diameter_when_written() -> None:
    meta = parse_metadata({"filament_total": 1000, "filament_density": "1.24;1.24", "filament_diameter": [1.75]})
    assert (meta.filament_density, meta.filament_diameter_mm) == (1.24, 1.75)
    meta = parse_metadata({"filament_density": "1.24;1.27"})  # two filaments that differ: unknown
    assert meta.filament_density is None
    assert parse_metadata(VESPA_META).filament_density is None  # Moonraker's usual metadata has none


# --------------------------------------------------------------------------- #
# Material chosen by hand
# --------------------------------------------------------------------------- #
def _density_row() -> dict:
    job = _job()
    job.record_filament(4000.0)
    job.filament_price_per_kg = TABLE["PLA"]
    job.filament_price_source = "generic"
    job.filament_type = "PLA"
    job.filament_type_source = "file_name"
    return {**job.summary(), "electricity_cost": 0.5, "notes": ["other"]}


def test_material_by_hand_weighs_again_by_density() -> None:
    row = _density_row()
    refresh_row_filament(row, None, TABLE, "EUR")
    assert row["filament_g"] == pytest.approx(round(_grams(4000, 1.24), 2))
    assert row["filament_g_source"] == GRAMS_DENSITY
    set_row_material(row, "PETG", None, TABLE, "EUR")
    grams = round(_grams(4000, 1.27), 2)
    assert row["filament_g"] == pytest.approx(grams)
    assert row["filament_cost"] == pytest.approx(round(grams * 20 / 1000, 4))
    assert row["print_cost"] == pytest.approx(round(0.5 + grams * 20 / 1000, 4))
    assert row["notes"] == [
        "other",
        "Filament weight from the length used and the typical density of PETG (1.27 g/cm³, 1.75 mm filament)",
        "Filament cost at the generic price for PETG (20 EUR/kg)",
    ]


def test_material_by_hand_replaces_the_slicer_weight_of_another_material() -> None:
    """A row saved by 0.15.1: the slicer weighed ASA; the print was in fact PETG."""
    row = {
        "filament_mm": 10000.0,
        "filament_g": 25.02,
        "filament_type": "ASA",
        "filament_type_source": "slicer",
        "filament_price_per_kg": 25.0,
        "filament_price_source": "generic",
        "filament_cost": 0.6255,
        "notes": ["Filament cost at the generic price for ASA (25 EUR/kg)"],
    }
    set_row_material(row, "PETG", None, TABLE, "EUR")
    assert row["filament_g"] == pytest.approx(round(_grams(10000, 1.27), 2))
    assert row["filament_g_source"] == GRAMS_DENSITY
    # Back to ASA: the slicer's weight again.
    set_row_material(row, "ASA", None, TABLE, "EUR")
    assert row["filament_g"] == pytest.approx(25.02)
    assert row["filament_g_source"] == GRAMS_SLICER
    assert row["notes"][0] == "Filament weight from the slicer"


def test_old_print_gets_its_length_and_cost() -> None:
    row = {"job_id": "a", "name": VESPA, "filament_mm": None, "electricity_cost": 1.0, "notes": []}
    row["notes"] = ["Filament used unknown: the printer did not report it"]
    meta = parse_metadata(VESPA_META)
    set_row_length(row, VESPA_USED, g_per_mm=meta.filament_g_per_mm, file_type=meta.filament_type)
    refresh_row_filament(row, None, TABLE, "EUR")
    grams = VESPA_USED * 280.68 / 112204.07
    assert row["filament_mm"] == pytest.approx(112232.6)
    assert row["filament_m"] == pytest.approx(112.23)
    assert row["filament_g"] == pytest.approx(round(grams, 2))
    assert (row["filament_type"], row["filament_type_source"]) == ("ASA", "slicer")
    assert row["filament_cost"] == pytest.approx(round(grams * 25 / 1000, 4))  # about 7.02 €
    assert row["print_cost"] == pytest.approx(round(1.0 + grams * 25 / 1000, 4))
    assert not any(n.startswith("Filament used unknown") for n in row["notes"])


# --------------------------------------------------------------------------- #
# Moonraker's history
# --------------------------------------------------------------------------- #
def test_history_match_by_file_and_end_time() -> None:
    end = T0.timestamp()
    jobs = [
        {"filename": VESPA, "end_time": end + 90, "filament_used": VESPA_USED, "metadata": VESPA_META},
        {"filename": VESPA, "end_time": end - 3600, "filament_used": 526.1},  # an earlier attempt
        {"filename": "other.gcode", "end_time": end, "filament_used": 99.0},
        {"filename": VESPA, "start_time": end - 60, "filament_used": 0.0, "status": "in_progress"},  # no end
    ]
    found = history_match(jobs, VESPA, end)
    assert found.filament_mm == pytest.approx(VESPA_USED)
    assert found.meta.filament_type == "ASA"
    assert found.meta.filament_g_per_mm == pytest.approx(280.68 / 112204.07)
    assert history_match(jobs, VESPA, end - 3600 + 30).filament_mm == pytest.approx(526.1)


def test_history_match_without_a_match_or_with_zero() -> None:
    end = T0.timestamp()
    late = [{"filename": VESPA, "end_time": end + HISTORY_END_TOLERANCE_S + 1, "filament_used": 500.0}]
    assert history_match(late, VESPA, end) is None  # too far from the end
    zero = [{"filename": VESPA, "end_time": end + 10, "filament_used": 0.0, "status": "klippy_disconnect"}]
    assert history_match(zero, VESPA, end) is None  # Klipper restarted: 0 is not a measurement
    assert history_match([], VESPA, end) is None
    assert history_match(None, VESPA, end) is None
    assert history_match(late, None, end) is None
    assert history_match(late, VESPA, None) is None


# --------------------------------------------------------------------------- #
# Google Sheets: an old print is changed in place, never added
# --------------------------------------------------------------------------- #
async def test_in_place_only_row_is_never_appended(hass: HomeAssistant) -> None:
    _google_sheets_entry(hass)
    calls = _sheets_service(hass)
    log = SheetsLog(hass, "e1", "Ender 6", SHEETS)
    row = {"job_id": "j1", "filament_mm": 1000.0, "notes": "Correction: x"}
    pending = [{**row, CORRECTION_KEY: list(LENGTH_CELLS), IN_PLACE_ONLY_KEY: True}]
    with patch(UPDATE_ROWS, return_value=0):  # the print is not in the sheet
        await log.async_flush(pending, AsyncMock())
    assert calls == []
    assert pending == []
    pending = [{**row, CORRECTION_KEY: list(LENGTH_CELLS), IN_PLACE_ONLY_KEY: True}]
    with patch(UPDATE_ROWS, return_value=1) as update:
        await log.async_flush(pending, AsyncMock())
    assert update.call_args.args[4] == {"filament_mm": 1000.0, "notes": "Correction: x"}
    assert pending == []


# --------------------------------------------------------------------------- #
# End to end (Moonraker mocked)
# --------------------------------------------------------------------------- #
CLIENT = "custom_components.fdm_printer_manager.coordinator.MoonrakerClient"
ENERGY = "sensor.escritorio_tomada_ender6_energy"
NO_WEIGHT = "bracket_v2.gcode"


class FakeMoonraker:
    def __init__(self) -> None:
        self.reading = KlipperStatus(state="standby")
        self.meta = parse_metadata({"estimated_time": 600, "filament_total": 10000.0, "filament_type": "ASA"})
        self.jobs: list[dict] = []
        self.history_calls: list[dict] = []

    async def status(self) -> KlipperStatus:
        return self.reading

    async def metadata(self, name: str):
        return self.meta

    async def history(self, limit: int = 10, since: float | None = None) -> list[dict]:
        self.history_calls.append({"limit": limit, "since": since})
        return self.jobs


@pytest.fixture
def moonraker():
    fake = FakeMoonraker()
    with patch.multiple(
        CLIENT,
        status=AsyncMock(side_effect=fake.status),
        history=AsyncMock(side_effect=fake.history),
        metadata=AsyncMock(side_effect=fake.metadata),
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
            "price_per_kwh": 0.2,
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


async def _history(hass: HomeAssistant) -> dict:
    connection = MagicMock()
    connection.user.is_admin = True
    ws_history(hass, connection, {"id": 1, "type": f"{DOMAIN}/history", "limit": 50, "offset": 0})
    await hass.async_block_till_done(wait_background_tasks=True)
    return connection.send_result.call_args.args[1]


async def test_print_without_slicer_weight_gets_grams_and_cost(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, moonraker
) -> None:
    freezer.move_to("2026-11-08 10:00:00+00:00")
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    moonraker.reading = KlipperStatus(state="printing", filename=NO_WEIGHT, filament_mm=0.0)
    await _poll(hass, freezer, 0.1)
    moonraker.reading = KlipperStatus(state="printing", filename=NO_WEIGHT, filament_mm=4000.0)
    await _poll(hass, freezer, 10)
    moonraker.reading = KlipperStatus(state="complete", filename=NO_WEIGHT, filament_mm=4000.0)
    await _poll(hass, freezer, 1)

    row = coordinator.state.history[0]
    grams = _grams(4000, 1.07)
    assert row["filament_g"] == pytest.approx(round(grams, 2))
    assert row["filament_g_source"] == GRAMS_DENSITY
    assert row["filament_cost"] == pytest.approx(round(grams * 25 / 1000, 4))
    assert (
        "Filament weight from the length used and the typical density of ASA (1.07 g/cm³, 1.75 mm filament)"
        in row["notes"]
    )
    assert not any("slicer did not write" in n for n in row["notes"])

    # Chosen by hand: PETG. The grams and the cost are worked out again, the sensor follows.
    job_id = row["job_id"]
    await hass.services.async_call(
        DOMAIN, "set_material", {"entry_id": entry.entry_id, "job_id": job_id, "material": "PETG"}, blocking=True
    )
    grams = _grams(4000, 1.27)
    row = coordinator.state.history[0]
    assert row["filament_g"] == pytest.approx(round(grams, 2))
    assert row["filament_cost"] == pytest.approx(round(grams * 20 / 1000, 4))
    assert coordinator.state.last_job.filament_g == pytest.approx(grams)
    sensor = hass.states.get("sensor.ender_6_last_print_filament")
    assert sensor.attributes["filament_g"] == pytest.approx(round(grams, 2))
    assert sensor.attributes["filament_cost"] == pytest.approx(round(grams * 20 / 1000, 4))


def _old_row(job_id: str, name: str, finished: datetime, **extra) -> dict:
    return {
        "printer": "Ender 6",
        "job_id": job_id,
        "name": name,
        "result": "success",
        "started_at": (finished - timedelta(hours=1)).isoformat(),
        "finished_at": finished.isoformat(),
        "electricity_cost": 1.0,
        "filament_mm": None,
        "filament_g": None,
        "filament_type": None,
        "filament_cost": None,
        "print_cost": 1.0,
        "notes": ["Filament used unknown: the printer did not report it"],
        **extra,
    }


async def test_old_prints_get_their_length_from_moonraker(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, moonraker
) -> None:
    freezer.move_to("2026-11-08 10:00:00+00:00")
    entry = await _setup(hass, **SHEETS)
    coordinator = entry.runtime_data
    end = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)
    coordinator.state.history[:] = [
        _old_row("a", VESPA, end),  # in Moonraker's history, ended 2 min later
        _old_row("b", "x_PETG.gcode", end - timedelta(hours=2)),  # only 20 min away: another print
        _old_row("c", "y.gcode", end - timedelta(hours=4)),  # Moonraker says 0 (Klipper restarted)
        _old_row("d", "z.gcode", end - timedelta(hours=6)),  # not in Moonraker's history
        # Length known but no slicer weight (0.15.1): weighed with the PLA of its file name.
        _old_row("e", "clip_PLA.gcode", end - timedelta(hours=8), filament_mm=5000.0, filament_m=5.0),
    ]
    # The print "a" was never sent to Google Sheets: its row in the queue is changed.
    coordinator.state.sheets_pending[:] = [{"job_id": "a", "filament_mm": "", "result": "success"}]
    ts = end.timestamp()
    moonraker.jobs = [
        {"filename": VESPA, "end_time": ts + 120, "filament_used": VESPA_USED, "metadata": VESPA_META},
        {"filename": "x_PETG.gcode", "end_time": ts - 7200 + 1200, "filament_used": 800.0},
        {"filename": "y.gcode", "end_time": ts - 14400 + 30, "filament_used": 0.0, "status": "klippy_shutdown"},
    ]

    result = await _history(hass)
    rows = {r["job_id"]: r for r in result["rows"]}
    grams = VESPA_USED * 280.68 / 112204.07
    assert rows["a"]["filament_m"] == pytest.approx(112.23)
    assert rows["a"]["filament_g"] == pytest.approx(round(grams, 2))
    assert rows["a"]["filament_type"] == "ASA"
    assert rows["a"]["filament_cost"] == pytest.approx(round(grams * 25 / 1000, 4))
    assert rows["a"]["print_cost"] == pytest.approx(round(1 + grams * 25 / 1000, 4))
    assert "Filament used read later from Moonraker's history" in rows["a"]["notes"]
    for job_id in ("b", "c", "d"):  # no match, or 0: unknown, never estimated
        assert rows[job_id]["filament_mm"] is None
        assert rows[job_id]["filament_cost"] is None
    assert rows["e"]["filament_g"] == pytest.approx(round(_grams(5000, 1.24), 2))
    assert rows["e"]["filament_type"] == "PLA"
    assert rows["e"]["filament_cost"] == pytest.approx(round(round(_grams(5000, 1.24), 2) * 20 / 1000, 4))

    # Saved in the history; Google Sheets: the waiting row of "a" changed, corrections in place only.
    assert coordinator.state.history[0]["filament_g"] == pytest.approx(round(grams, 2))
    pending = coordinator.state.sheets_pending
    assert pending[0]["job_id"] == "a"
    assert pending[0]["filament_mm"] == pytest.approx(112232.6)
    assert CORRECTION_KEY not in pending[0]
    assert all(p.get(IN_PLACE_ONLY_KEY) for p in pending[1:])

    # Read once: the panel asking again does not read Moonraker's history again.
    calls = len(moonraker.history_calls)
    assert moonraker.history_calls[-1]["since"] is not None
    await _history(hass)
    assert len(moonraker.history_calls) == calls


async def test_last_print_follows_its_length_from_moonraker(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, moonraker
) -> None:
    """A print that ended with Klipper down (no length): filled in later, the last-print sensor too."""
    freezer.move_to("2026-11-08 10:00:00+00:00")
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    moonraker.meta = parse_metadata({"estimated_time": 600, **VESPA_META})
    moonraker.reading = KlipperStatus(state="printing", filename=VESPA)  # no filament_used (old Moonraker)
    await _poll(hass, freezer, 0.1)
    moonraker.reading = KlipperStatus(state="complete", filename=VESPA)
    await _poll(hass, freezer, 30)
    row = coordinator.state.history[0]
    assert row["filament_mm"] is None
    end = datetime.fromisoformat(row["finished_at"]).timestamp()
    moonraker.jobs = [{"filename": VESPA, "end_time": end + 5, "filament_used": 2000.0, "metadata": VESPA_META}]
    # Read when it ended: not looked up again while Home Assistant runs.
    assert await coordinator.async_fill_filament_lengths() == 0
    coordinator._length_looked.clear()  # Home Assistant restarted

    assert await coordinator.async_fill_filament_lengths() == 1
    await hass.async_block_till_done()
    last = coordinator.state.last_job
    assert last.filament_mm == pytest.approx(2000.0)
    assert last.filament_g == pytest.approx(2000.0 * 280.68 / 112204.07)
    assert not any(r.startswith("Filament used unknown") for r in last.remarks)
    sensor = hass.states.get("sensor.ender_6_last_print_filament")
    assert float(sensor.state) == pytest.approx(2.0)
