"""0.15.0: metres of filament, the material of every print, and a generic price per material."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.fdm_printer_manager import ws_history
from custom_components.fdm_printer_manager.adapters.moonraker import KlipperStatus, parse_metadata
from custom_components.fdm_printer_manager.const import DOMAIN
from custom_components.fdm_printer_manager.coordinator import PrintCostCoordinator
from custom_components.fdm_printer_manager.filament import FilamentSlot
from custom_components.fdm_printer_manager.materials import (
    DEFAULT_PRICES,
    MATERIALS,
    PRICE_GENERIC,
    PRICE_PRINTER,
    generic_prices,
    material_from_file_name,
    normalize_material,
    price_key,
    resolve_price,
    set_row_material,
)
from custom_components.fdm_printer_manager.models import ElectricityAccumulator, PrintJob
from custom_components.fdm_printer_manager.sheets import (
    CORRECTION_KEY,
    MATERIAL_CELLS,
    correct_pending_row,
    correction_cells,
    mark_pending_row,
)

CLIENT = "custom_components.fdm_printer_manager.coordinator.MoonrakerClient"
ENERGY = "sensor.escritorio_tomada_ender6_energy"
# A file whose slicer did not write the filament type, and whose name says nothing either.
PLAIN = "bracket_v2.gcode"
PLAIN_META = {"filament_total": 10000.0, "filament_weight_total": 25.0}  # 2.5 g/m


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "material"),
    [
        ("PLA", "PLA"),
        ("PLA+", "PLA"),
        ("Bambu PLA Basic", "PLA"),
        ("Generic PETG", "PETG"),
        ("PETG HF", "PETG"),
        ("PET-G", "PETG"),
        ("PETG-CF", "PETG-CF"),
        ("PETG CF", "PETG-CF"),
        ("PETGCF", "PETG-CF"),
        ("PET-CF", "PET-CF"),
        ("PET", "PET"),
        ("PLA-CF", "PLA-CF"),
        ("abs", "ABS"),
        ("ASA", "ASA"),
        ("ASA;ASA", "ASA"),
        ("TPU 95A", "TPU"),
        ("TPU95A", "TPU"),
        ("PC", "PC"),
        ("Nylon", "PA"),
        ("PA6-CF", "PA"),
        ("PA12", "PA"),
        ("PAHT-CF", "PA"),
        ("PP", "PP"),
        ("PPA-CF", "PPA"),
        ("PPS", "PPS"),
        ("PLA;PETG", None),  # two materials: unknown, never a guess
        ("HIPS", None),
        ("PVA", None),
        ("", None),
        (None, None),
    ],
)
def test_material_names_are_made_the_same(text, material) -> None:
    assert normalize_material(text) == material


def test_material_from_the_file_name() -> None:
    assert material_from_file_name("Vespa_tampao_V0_v1 (1)_ASA_14h37m.gcode") == "ASA"
    assert material_from_file_name("clip_PETG_0.2mm.gcode") == "PETG"
    assert material_from_file_name("bracket_v2.gcode") is None
    assert material_from_file_name("PC_stand_PLA.gcode") is None  # two names: unknown
    assert material_from_file_name(None) is None


def test_the_list_asked_for() -> None:
    assert set(MATERIALS) == {
        "ABS",
        "ASA",
        "PC",
        "PA",
        "PLA",
        "PET",
        "PETG",
        "PET-CF",
        "PLA-CF",
        "PETG-CF",
        "PP",
        "PPA",
        "PPS",
        "TPU",
    }
    assert set(DEFAULT_PRICES) == set(MATERIALS)


# --------------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------------- #
def test_generic_prices_from_the_options() -> None:
    assert generic_prices({})["ASA"] == Decimal(25)
    table = generic_prices({price_key("ASA"): 30, price_key("PETG-CF"): None, "price_pla": "abc"})
    assert table["ASA"] == Decimal(30)
    assert "PETG-CF" not in table  # cleared: no generic price for it
    assert table["PLA"] == Decimal(20)  # unreadable: the default stays
    assert price_key("PETG-CF") == "price_petg_cf"


def test_price_order_printer_then_material_then_nothing() -> None:
    table = generic_prices({})
    assert resolve_price(Decimal(18), "ASA", table) == (Decimal(18), PRICE_PRINTER)
    assert resolve_price(None, "ASA", table) == (Decimal(25), PRICE_GENERIC)
    assert resolve_price(None, None, table) == (None, None)
    assert resolve_price(None, "WOOD", table) == (None, None)


def test_model_summary_has_metres_and_the_print_cost() -> None:
    from datetime import UTC, datetime  # noqa: PLC0415

    job = PrintJob(printer_id="p", started_at=datetime(2026, 11, 8, tzinfo=UTC), electricity=ElectricityAccumulator())
    job.record_filament(112204.07)
    job.set_filament_file(280.68 / 112204.07, "Generic ASA")
    job.filament_price_per_kg = Decimal(25)
    job.filament_price_source = PRICE_GENERIC
    summary = job.summary()
    assert summary["filament_m"] == pytest.approx(112.2, abs=0.01)
    assert summary["filament_type"] == "ASA"
    assert summary["filament_type_source"] == "slicer"
    assert summary["filament_cost"] == pytest.approx(280.68 * 25 / 1000, abs=1e-3)
    assert summary["print_cost"] == pytest.approx(summary["filament_cost"], abs=1e-3)  # no electricity known
    restored = PrintJob.from_dict(job.as_dict())
    assert (restored.filament_type_source, restored.filament_price_source) == ("slicer", PRICE_GENERIC)


def test_a_material_chosen_by_hand_recomputes_the_cost() -> None:
    row = {
        "filament_g": 100.0,
        "filament_type": None,
        "electricity_cost": 0.5,
        "filament_cost": None,
        "notes": ["Filament cost unknown: material unknown (choose it in Recent prints)", "other"],
    }
    set_row_material(row, "PETG", None, generic_prices({}), "EUR")
    assert row["filament_type"] == "PETG"
    assert row["filament_type_source"] == "user"
    assert row["filament_cost"] == pytest.approx(2.0)
    assert row["filament_price_source"] == PRICE_GENERIC
    assert row["print_cost"] == pytest.approx(2.5)
    assert row["notes"] == ["other", "Filament cost at the generic price for PETG (20 EUR/kg)"]
    # Changed again: the note follows, never two.
    set_row_material(row, "ASA", None, generic_prices({}), "EUR")
    assert row["filament_cost"] == pytest.approx(2.5)
    assert row["notes"] == ["other", "Filament cost at the generic price for ASA (25 EUR/kg)"]
    # A price set on the printer when the print ended stays: it does not depend on the material.
    row["filament_price_source"] = PRICE_PRINTER
    row["filament_price_per_kg"] = 30.0
    set_row_material(row, "PLA", None, generic_prices({}), "EUR")
    assert row["filament_cost"] == pytest.approx(3.0)
    assert row["notes"] == ["other"]


# --------------------------------------------------------------------------- #
# Google Sheets corrections
# --------------------------------------------------------------------------- #
def test_a_material_correction_changes_only_its_cells() -> None:
    pending = [{"job_id": "j1", "result": "success", "filament_type": ""}]
    assert correct_pending_row(pending, "j1", {"filament_type": "PETG", "filament_cost": None})
    assert pending[0]["filament_type"] == "PETG"
    assert pending[0]["filament_cost"] == ""
    assert CORRECTION_KEY not in pending[0]  # not sent yet: the whole row goes as a normal row
    assert not correct_pending_row(pending, "other", {"filament_type": "PLA"})

    correction = {"job_id": "j2", "result": "success", CORRECTION_KEY: list(MATERIAL_CELLS)}
    assert correction_cells(correction) == MATERIAL_CELLS
    assert correction_cells({CORRECTION_KEY: True}) == ("result", "result_by")  # queued before 0.15.0
    # Marked by hand while the material correction waits: both are changed in place.
    assert mark_pending_row([correction], "j2", "failed")
    assert set(correction_cells(correction)) == {"result", "result_by", *MATERIAL_CELLS}


# --------------------------------------------------------------------------- #
# End to end (Moonraker mocked)
# --------------------------------------------------------------------------- #
class FakeMoonraker:
    def __init__(self) -> None:
        self.reading = KlipperStatus(state="standby")
        self.meta = parse_metadata({"estimated_time": 600, **PLAIN_META})

    async def status(self) -> KlipperStatus:
        return self.reading

    async def metadata(self, name: str):
        return self.meta


@pytest.fixture
def moonraker():
    fake = FakeMoonraker()
    with patch.multiple(
        CLIENT,
        status=AsyncMock(side_effect=fake.status),
        history=AsyncMock(return_value=[]),
        metadata=AsyncMock(side_effect=fake.metadata),
        webcams=AsyncMock(return_value=[]),
    ):
        yield fake


async def _setup(hass: HomeAssistant, panel_options: dict | None = None, **options) -> MockConfigEntry:
    if panel_options is not None:
        MockConfigEntry(domain=DOMAIN, title="FDM Printers", unique_id="panel", data={"panel": True}).add_to_hass(hass)
        panel = hass.config_entries.async_entries(DOMAIN)[0]
        hass.config_entries.async_update_entry(panel, options=panel_options)
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


async def _print(hass: HomeAssistant, freezer: FrozenDateTimeFactory, moonraker, name: str = PLAIN) -> None:
    moonraker.reading = KlipperStatus(state="printing", filename=name, filament_mm=0.0)
    await _poll(hass, freezer, 0.1)
    moonraker.reading = KlipperStatus(state="printing", filename=name, filament_mm=4000.0)
    await _poll(hass, freezer, 10)
    moonraker.reading = KlipperStatus(state="complete", filename=name, filament_mm=4000.0)
    await _poll(hass, freezer, 1)


async def test_unknown_material_stays_unknown(hass: HomeAssistant, freezer: FrozenDateTimeFactory, moonraker) -> None:
    freezer.move_to("2026-11-08 10:00:00+00:00")
    entry = await _setup(hass)
    await _print(hass, freezer, moonraker)
    row = entry.runtime_data.state.history[0]
    assert row["filament_m"] == pytest.approx(4.0)
    assert row["filament_g"] == pytest.approx(10.0)
    assert row["filament_type"] is None  # "?" in the panel, never invented
    assert row["filament_cost"] is None
    assert "Filament cost unknown: material unknown (choose it in Recent prints)" in row["notes"]
    sensor = hass.states.get("sensor.ender_6_last_print_filament")
    assert float(sensor.state) == pytest.approx(4.0)
    assert sensor.attributes["unit_of_measurement"] == "m"
    assert sensor.attributes["filament_g"] == pytest.approx(10.0)


async def test_material_from_the_spool_loaded_while_printing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, moonraker
) -> None:
    freezer.move_to("2026-11-08 10:00:00+00:00")
    slots = [
        FilamentSlot(index=0, name="CANVAS_1", material="PETG", active=True),
        FilamentSlot(index=1, name="CANVAS_2", material="PLA"),
    ]
    with patch.object(PrintCostCoordinator, "filament_slots", new_callable=PropertyMock, return_value=slots):
        entry = await _setup(hass)
        await _print(hass, freezer, moonraker)
    row = entry.runtime_data.state.history[0]
    assert row["filament_type"] == "PETG"
    assert row["filament_type_source"] == "spool"
    assert row["filament_cost"] == pytest.approx(10.0 * 20 / 1000)
    assert row["filament_price_source"] == "generic"


async def test_material_from_the_file_name_and_the_printer_price_wins(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, moonraker
) -> None:
    freezer.move_to("2026-11-08 10:00:00+00:00")
    entry = await _setup(hass, filament_price_per_kg=40)
    await _print(hass, freezer, moonraker, "clip_ASA_0.2mm.gcode")
    row = entry.runtime_data.state.history[0]
    assert (row["filament_type"], row["filament_type_source"]) == ("ASA", "file_name")
    assert row["filament_cost"] == pytest.approx(10.0 * 40 / 1000)
    assert row["filament_price_source"] == "printer"
    assert not any("generic" in note for note in row["notes"])


async def test_generic_prices_come_from_fdm_printers(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, moonraker
) -> None:
    freezer.move_to("2026-11-08 10:00:00+00:00")
    moonraker.meta = parse_metadata({"estimated_time": 600, **PLAIN_META, "filament_type": "Generic ASA"})
    entry = await _setup(hass, panel_options={price_key("ASA"): 32})
    await _print(hass, freezer, moonraker)
    row = entry.runtime_data.state.history[0]
    assert (row["filament_type"], row["filament_type_source"]) == ("ASA", "slicer")
    assert row["filament_cost"] == pytest.approx(10.0 * 32 / 1000)
    assert "Filament cost at the generic price for ASA (32 EUR/kg)" in row["notes"]


async def test_choose_the_material_of_a_print(hass: HomeAssistant, freezer: FrozenDateTimeFactory, moonraker) -> None:
    freezer.move_to("2026-11-08 10:00:00+00:00")
    entry = await _setup(hass, sheets_config_entry="01SHEETSENTRY", sheets_worksheet="Prints")
    coordinator = entry.runtime_data
    await _print(hass, freezer, moonraker)
    job_id = coordinator.state.history[0]["job_id"]
    # Google Sheets is not set up here: the row waits in the queue, and is changed there.
    assert coordinator.state.sheets_pending[0]["filament_type"] == ""

    await hass.services.async_call(
        DOMAIN, "set_material", {"entry_id": entry.entry_id, "job_id": job_id, "material": "PETG"}, blocking=True
    )
    row = coordinator.state.history[0]
    assert (row["filament_type"], row["filament_type_source"]) == ("PETG", "user")
    assert row["filament_cost"] == pytest.approx(0.2)
    assert coordinator.state.last_job.filament_type == "PETG"
    sensor = hass.states.get("sensor.ender_6_last_print_filament")
    assert sensor.attributes["filament_type"] == "PETG"
    assert sensor.attributes["filament_cost"] == pytest.approx(0.2)
    assert coordinator.state.sheets_pending[0]["filament_type"] == "PETG"
    assert coordinator.state.sheets_pending[0]["filament_cost"] == pytest.approx(0.2)
    assert len(coordinator.state.sheets_pending) == 1

    # A wrong material can be changed again.
    await hass.services.async_call(
        DOMAIN, "set_material", {"entry_id": entry.entry_id, "job_id": job_id, "material": "ASA"}, blocking=True
    )
    assert coordinator.state.history[0]["filament_cost"] == pytest.approx(0.25)

    # The panel's history gets the list of materials with their prices.
    connection = MagicMock()
    connection.user.is_admin = True
    ws_history(hass, connection, {"id": 1, "type": f"{DOMAIN}/history", "limit": 50, "offset": 0})
    await hass.async_block_till_done()
    result = connection.send_result.call_args.args[1]
    assert [m["name"] for m in result["materials"]] == list(MATERIALS)
    assert next(m for m in result["materials"] if m["name"] == "PA")["label"] == "PA (Nylon)"
    assert result["rows"][0]["filament_type"] == "ASA"


async def test_set_material_refuses_bad_input(hass: HomeAssistant, freezer: FrozenDateTimeFactory, moonraker) -> None:
    import voluptuous as vol  # noqa: PLC0415
    from homeassistant.exceptions import HomeAssistantError, ServiceValidationError  # noqa: PLC0415

    freezer.move_to("2026-11-08 10:00:00+00:00")
    entry = await _setup(hass)
    with pytest.raises((vol.Invalid, ServiceValidationError)):
        await hass.services.async_call(
            DOMAIN, "set_material", {"entry_id": entry.entry_id, "job_id": "x", "material": "WOOD"}, blocking=True
        )
    with pytest.raises(HomeAssistantError, match="not found"):
        await hass.services.async_call(
            DOMAIN, "set_material", {"entry_id": entry.entry_id, "job_id": "x", "material": "PLA"}, blocking=True
        )


async def test_fdm_printers_options_hold_the_prices(hass: HomeAssistant) -> None:
    panel = MockConfigEntry(domain=DOMAIN, title="FDM Printers", unique_id="panel", data={"panel": True})
    panel.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(panel.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "panel"
    user_input = {price_key(m): float(DEFAULT_PRICES[m]) for m in MATERIALS}
    user_input[price_key("ASA")] = 27.5
    user_input.pop(price_key("PPS"))  # cleared
    user_input["finish_notify_services"] = " notify.mobile_app_phone "
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert panel.options["finish_notify_services"] == "notify.mobile_app_phone"
    table = generic_prices(dict(panel.options))
    assert table["ASA"] == Decimal("27.5")
    assert "PPS" not in table
