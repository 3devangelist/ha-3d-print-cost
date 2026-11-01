"""One row per finished print in a Google Sheet (google_sheets.append_sheet)."""

from datetime import UTC, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from custom_components.fdm_printer_manager.adapters.moonraker import KlipperStatus
from custom_components.fdm_printer_manager.const import DOMAIN, EVENT_PRINT_FINISHED
from custom_components.fdm_printer_manager.models import JobPhase, PrintResult
from custom_components.fdm_printer_manager.sheets import COLUMNS, PENDING_LIMIT, queue_row, sheet_row
from custom_components.fdm_printer_manager.storage import JobStore

from .test_klipper_integration import FILE, poll, printer, setup_ender  # noqa: F401 - printer is a fixture

SHEETS = {"sheets_config_entry": "01SHEETSENTRY", "sheets_worksheet": "Prints"}
NOTIFY = "custom_components.fdm_printer_manager.sheets.persistent_notification"


# --------------------------------------------------------------------------- #
# The row
# --------------------------------------------------------------------------- #
def test_sheet_row_columns_and_values() -> None:
    payload = {
        "printer": "Ender 6",
        "currency": "EUR",
        "job_id": "abc",
        "name": "cover.gcode",
        "result": "cancelled",
        "result_by": None,
        "started_at": "2026-10-28T10:00:00+00:00",
        "finished_at": "2026-10-28T11:30:00+00:00",
        "duration_s": 5400,
        "energy_kwh": 0.1234,
        "electricity_cost": 0.0197,
        "notes": ["12 s of the print had no fresh sample", "Interrupted"],
    }
    row = sheet_row(payload, ZoneInfo("Europe/Lisbon"))
    assert tuple(row) == COLUMNS
    assert row == {
        "printer": "Ender 6",
        "file": "cover.gcode",
        "result": "cancelled",
        "result_by": "printer",
        "started": "2026-10-28 10:00:00",  # Lisbon is UTC+0 in winter
        "finished": "2026-10-28 11:30:00",
        "duration_min": 90.0,
        "energy_kwh": 0.1234,
        "electricity_cost": 0.0197,
        "currency": "EUR",
        "notes": "12 s of the print had no fresh sample; Interrupted",
        "job_id": "abc",
    }


def test_sheet_row_missing_values_and_correction() -> None:
    row = sheet_row({"printer": "X"}, UTC, correction="Correction: marked as failed by hand (was success)")
    assert row["result"] == "unknown"
    assert row["file"] == ""
    assert row["started"] == ""
    assert row["duration_min"] == ""
    assert row["electricity_cost"] == ""
    assert row["notes"] == "Correction: marked as failed by hand (was success)"
    # Summer time is applied
    assert sheet_row({"started_at": "2026-07-01T10:00:00+00:00"}, ZoneInfo("Europe/Lisbon"))["started"] == (
        "2026-07-01 11:00:00"
    )


def test_queue_keeps_the_newest_rows() -> None:
    pending: list[dict] = []
    for i in range(PENDING_LIMIT + 5):
        queue_row(pending, {"job_id": str(i)})
    assert len(pending) == PENDING_LIMIT
    assert pending[0]["job_id"] == "5"
    assert pending[-1]["job_id"] == str(PENDING_LIMIT + 4)


# --------------------------------------------------------------------------- #
# End of a print
# --------------------------------------------------------------------------- #
def _sheets_service(hass: HomeAssistant, *, fail: bool = False) -> list[ServiceCall]:
    calls: list[ServiceCall] = []

    async def _append(call: ServiceCall) -> None:
        if fail:
            raise HomeAssistantError("Worksheet 'Prints' not found")
        calls.append(call)

    hass.services.async_register("google_sheets", "append_sheet", _append)
    return calls


async def _print(hass, freezer, printer, end_state: str, energy_wh: float = 10500) -> None:  # noqa: F811
    printer.reading = KlipperStatus(state="printing", filename=FILE, progress=0.0, print_duration_s=0)
    await poll(hass, freezer, 0.1, energy_wh - 500)
    printer.reading = KlipperStatus(state=end_state, filename=FILE, progress=100.0, print_duration_s=3600)
    await poll(hass, freezer, 60, energy_wh)
    printer.reading = KlipperStatus(state="standby")
    await poll(hass, freezer, 1, energy_wh)


async def test_finished_print_adds_one_row(hass: HomeAssistant, freezer: FrozenDateTimeFactory, printer) -> None:  # noqa: F811
    calls = _sheets_service(hass)
    freezer.move_to("2026-10-28 10:00:00+00:00")
    entry = await setup_ender(hass, **SHEETS)
    await _print(hass, freezer, printer, "complete")

    assert len(calls) == 1
    assert calls[0].data["config_entry"] == "01SHEETSENTRY"
    assert calls[0].data["worksheet"] == "Prints"
    row = calls[0].data["data"]
    assert tuple(row) == COLUMNS
    assert row["printer"] == "Ender 6"
    assert row["file"] == FILE
    assert row["result"] == "success"
    assert row["currency"] == entry.runtime_data.currency
    assert row["energy_kwh"] == pytest.approx(0.5)
    assert row["job_id"] == entry.runtime_data.state.history[0]["job_id"]
    assert entry.runtime_data.state.sheets_pending == []


async def test_cancelled_print_adds_one_cancelled_row(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    printer,  # noqa: F811
) -> None:
    calls = _sheets_service(hass)
    freezer.move_to("2026-10-28 10:00:00+00:00")
    await setup_ender(hass, **SHEETS)
    await _print(hass, freezer, printer, "cancelled")
    assert [c.data["data"]["result"] for c in calls] == ["cancelled"]

    await _print(hass, freezer, printer, "error", energy_wh=11000)
    assert [c.data["data"]["result"] for c in calls] == ["cancelled", "failed"]


async def test_off_when_no_sheet_chosen(hass: HomeAssistant, freezer: FrozenDateTimeFactory, printer) -> None:  # noqa: F811
    calls = _sheets_service(hass)
    freezer.move_to("2026-10-28 10:00:00+00:00")
    entry = await setup_ender(hass)
    await _print(hass, freezer, printer, "complete")
    assert calls == []
    assert entry.runtime_data.state.sheets_pending == []


async def test_failed_append_does_not_stop_the_print_end(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    printer,  # noqa: F811
) -> None:
    finished = []
    hass.bus.async_listen(EVENT_PRINT_FINISHED, finished.append)
    freezer.move_to("2026-10-28 10:00:00+00:00")
    with patch(NOTIFY) as notify:
        # No google_sheets integration at all: the action does not exist.
        entry = await setup_ender(hass, **SHEETS)
        await _print(hass, freezer, printer, "complete")

        # The print was stored and announced as usual.
        assert len(finished) == 1
        assert hass.states.get("sensor.ender_6_last_print_result").state == "success"
        coordinator = entry.runtime_data
        assert len(coordinator.state.history) == 1
        assert coordinator.state.finalizing == []
        # The row waits, also on disk; one notification.
        assert len(coordinator.state.sheets_pending) == 1
        stored = await JobStore(hass, entry.entry_id).async_load()
        assert stored.sheets_pending[0]["result"] == "success"
        assert notify.async_create.call_count == 1

        # Still failing at the next print: two rows wait, still one notification.
        calls = _sheets_service(hass, fail=True)
        await _print(hass, freezer, printer, "cancelled", energy_wh=11000)
        assert len(coordinator.state.sheets_pending) == 2
        assert notify.async_create.call_count == 1
        assert calls == []

        # Google Sheets works again: both rows go, oldest first, and the notification goes away.
        hass.services.async_remove("google_sheets", "append_sheet")
        calls = _sheets_service(hass)
        await _print(hass, freezer, printer, "complete", energy_wh=11500)
        assert [c.data["data"]["result"] for c in calls] == ["success", "cancelled", "success"]
        assert coordinator.state.sheets_pending == []
        assert notify.async_dismiss.call_count == 1


async def test_waiting_rows_sent_after_restart(hass: HomeAssistant, freezer: FrozenDateTimeFactory, printer) -> None:  # noqa: F811
    freezer.move_to("2026-10-28 10:00:00+00:00")
    with patch(NOTIFY):
        entry = await setup_ender(hass, **SHEETS)
        await _print(hass, freezer, printer, "cancelled")
    assert len(entry.runtime_data.state.sheets_pending) == 1

    calls = _sheets_service(hass)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [c.data["data"]["result"] for c in calls] == ["cancelled"]
    assert entry.runtime_data.state.sheets_pending == []


async def test_print_ending_during_restart_logged_once(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    printer,  # noqa: F811
) -> None:
    """A print still being finalised when HA stopped is finalised at start: one row."""
    calls = _sheets_service(hass)
    freezer.move_to("2026-10-28 10:00:00+00:00")
    entry = await setup_ender(hass, **SHEETS)
    printer.reading = KlipperStatus(state="printing", filename=FILE, progress=0.0, print_duration_s=0)
    await poll(hass, freezer, 0.1, 10000)
    coordinator = entry.runtime_data
    job = coordinator.tracker.active_job
    job.finished_at = datetime(2026, 10, 28, 11, 0, tzinfo=UTC)
    job.result = PrintResult.CANCELLED
    job.phase = JobPhase.FINALIZING
    coordinator.tracker.active_job = None
    coordinator.state.active_job = None
    coordinator.state.finalizing = [job]
    # HA stops before the end of the print is stored: the job is saved as "finalizing".
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert len((await JobStore(hass, entry.entry_id).async_load()).finalizing) == 1
    printer.reading = KlipperStatus(state="cancelled", filename=FILE)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert [c.data["data"]["result"] for c in calls] == ["cancelled"]
    assert len(entry.runtime_data.state.history) == 1


async def test_mark_print_adds_a_correction_row(hass: HomeAssistant, freezer: FrozenDateTimeFactory, printer) -> None:  # noqa: F811
    calls = _sheets_service(hass)
    freezer.move_to("2026-10-28 10:00:00+00:00")
    entry = await setup_ender(hass, **SHEETS)
    await _print(hass, freezer, printer, "complete")
    job_id = calls[0].data["data"]["job_id"]

    await hass.services.async_call(
        DOMAIN, "mark_print", {"entry_id": entry.entry_id, "result": "failed"}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(calls) == 2
    row = calls[1].data["data"]
    assert row["job_id"] == job_id
    assert row["result"] == "failed"
    assert row["result_by"] == "user"
    assert row["notes"].startswith("Correction: marked as failed by hand (was success)")
    assert row["energy_kwh"] == calls[0].data["data"]["energy_kwh"]

    # Same result again: nothing new.
    await hass.services.async_call(
        DOMAIN, "mark_print", {"entry_id": entry.entry_id, "result": "failed", "job_id": job_id}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(calls) == 2
