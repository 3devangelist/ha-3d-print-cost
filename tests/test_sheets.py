"""One row per finished print in a Google Sheet (google_sheets.append_sheet)."""

import sys
import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockModule, mock_integration

from custom_components.fdm_printer_manager.adapters.moonraker import KlipperStatus
from custom_components.fdm_printer_manager.const import DOMAIN, EVENT_PRINT_FINISHED
from custom_components.fdm_printer_manager.models import JobPhase, PrintResult
from custom_components.fdm_printer_manager.sheets import (
    COLUMNS,
    PENDING_LIMIT,
    SheetsLog,
    mark_pending_row,
    queue_row,
    sheet_row,
    update_rows,
)
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


# --------------------------------------------------------------------------- #
# 0.12.3: marking a print again changes its row in the sheet
# --------------------------------------------------------------------------- #
UPDATE_ROWS = "custom_components.fdm_printer_manager.sheets.update_rows"


def _google_sheets_entry(hass: HomeAssistant) -> MockConfigEntry:
    """The google_sheets entry the printer logs to (SHEETS), loaded, with its Google login."""
    mock_integration(hass, MockModule("google_sheets"))
    entry = MockConfigEntry(
        domain="google_sheets",
        entry_id="01SHEETSENTRY",
        unique_id="SHEET123",
        data={"token": {"access_token": "token-1"}},
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = Mock(async_ensure_token_valid=AsyncMock())
    return entry


async def _mark(hass: HomeAssistant, entry, result: str) -> None:
    await hass.services.async_call(DOMAIN, "mark_print", {"entry_id": entry.entry_id, "result": result}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_mark_print_changes_the_row_in_the_sheet(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    printer,  # noqa: F811
) -> None:
    calls = _sheets_service(hass)
    _google_sheets_entry(hass)
    freezer.move_to("2026-10-28 10:00:00+00:00")
    entry = await setup_ender(hass, **SHEETS)
    await _print(hass, freezer, printer, "complete")
    job_id = calls[0].data["data"]["job_id"]

    with patch(UPDATE_ROWS, return_value=1) as update:
        await _mark(hass, entry, "failed")
        update.assert_called_once_with(
            "token-1", "SHEET123", "Prints", job_id, {"result": "failed", "result_by": "user"}
        )
        await _mark(hass, entry, "success")  # every press: the same row again
        assert update.call_args.args[4] == {"result": "success", "result_by": "user"}
    assert len(calls) == 1  # no correction row
    assert entry.runtime_data.state.sheets_pending == []


@pytest.mark.parametrize("update", [Mock(return_value=0), Mock(side_effect=RuntimeError("403 no access"))])
async def test_mark_print_adds_a_correction_row_when_the_row_cannot_be_changed(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    printer,  # noqa: F811
    update: Mock,
) -> None:
    """Row not found (0) or no access to the sheet: the correction row of 0.11.0, sent through append_sheet."""
    calls = _sheets_service(hass)
    _google_sheets_entry(hass)
    freezer.move_to("2026-10-28 10:00:00+00:00")
    entry = await setup_ender(hass, **SHEETS)
    await _print(hass, freezer, printer, "complete")
    update.reset_mock()
    with patch(UPDATE_ROWS, update):
        await _mark(hass, entry, "failed")
    assert update.call_count == 1
    assert len(calls) == 2
    row = calls[1].data["data"]
    assert tuple(row) == COLUMNS  # the queue marker is not sent
    assert row["result"] == "failed"
    assert row["notes"].startswith("Correction: marked as failed by hand (was success)")
    assert entry.runtime_data.state.sheets_pending == []


async def test_correction_waits_in_the_queue_when_nothing_works(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    printer,  # noqa: F811
) -> None:
    calls = _sheets_service(hass)
    _google_sheets_entry(hass)
    freezer.move_to("2026-10-28 10:00:00+00:00")
    entry = await setup_ender(hass, **SHEETS)
    await _print(hass, freezer, printer, "complete")
    hass.services.async_remove("google_sheets", "append_sheet")
    with patch(NOTIFY), patch(UPDATE_ROWS, side_effect=RuntimeError("offline")):
        await _mark(hass, entry, "failed")
    pending = entry.runtime_data.state.sheets_pending
    assert len(pending) == 1
    assert pending[0]["_correction"] is True
    stored = await JobStore(hass, entry.entry_id).async_load()
    assert stored.sheets_pending[0]["_correction"] is True  # kept on disk for the retry

    # Back online: the row in the sheet is changed, no correction row.
    calls = _sheets_service(hass)
    with patch(UPDATE_ROWS, return_value=1) as update:
        await entry.runtime_data.sheets.async_flush(pending, AsyncMock())
    assert update.call_args.args[4] == {"result": "failed", "result_by": "user"}
    assert calls == []
    assert pending == []


async def test_mark_print_changes_a_row_still_in_the_queue(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    printer,  # noqa: F811
) -> None:
    """The print's row was not sent yet: it is changed in the queue, one row only, nothing to correct."""
    freezer.move_to("2026-10-28 10:00:00+00:00")
    _google_sheets_entry(hass)
    with patch(NOTIFY):
        entry = await setup_ender(hass, **SHEETS)  # no append_sheet action: the row waits
        await _print(hass, freezer, printer, "complete")
        pending = entry.runtime_data.state.sheets_pending
        assert len(pending) == 1
        await _mark(hass, entry, "failed")
    assert len(pending) == 1
    assert pending[0]["result"] == "failed"
    assert pending[0]["result_by"] == "user"
    assert "_correction" not in pending[0]

    calls = _sheets_service(hass)
    with patch(UPDATE_ROWS) as update:
        await entry.runtime_data.sheets.async_flush(pending, AsyncMock())
    update.assert_not_called()
    assert [c.data["data"]["result"] for c in calls] == ["failed"]
    assert pending == []


async def test_marked_while_being_sent_is_corrected_next(hass: HomeAssistant) -> None:
    """The row goes out with the old result while it is marked: the sheet's row is changed right after."""
    _google_sheets_entry(hass)
    log = SheetsLog(hass, "e1", "Ender 6", SHEETS)
    pending = [{"job_id": "j1", "result": "success", "result_by": "printer", "notes": ""}]
    sent: list[dict] = []

    async def _append(call: ServiceCall) -> None:
        sent.append(dict(call.data["data"]))
        assert mark_pending_row(pending, "j1", "failed")  # pressed now

    hass.services.async_register("google_sheets", "append_sheet", _append)
    with patch(UPDATE_ROWS, return_value=1) as update:
        await log.async_flush(pending, AsyncMock())
    assert [r["result"] for r in sent] == ["success"]
    update.assert_called_once_with("token-1", "SHEET123", "Prints", "j1", {"result": "failed", "result_by": "user"})
    assert pending == []


def test_mark_pending_row_rewrites_a_waiting_correction() -> None:
    pending = [
        {"job_id": "j1", "result": "success", "notes": ""},
        {"job_id": "j2", "result": "failed", "notes": "Correction: marked as failed by hand (was success); a note"},
    ]
    pending[1]["_correction"] = True
    assert mark_pending_row(pending, "j2", "success")
    assert pending[1]["notes"] == "Correction: marked as success by hand (was success); a note"
    assert pending[0]["result"] == "success"
    assert not mark_pending_row(pending, "j3", "failed")


def test_update_rows_changes_every_row_of_the_print() -> None:
    class Worksheet:
        def __init__(self) -> None:
            self.updates: list = []

        def row_values(self, row: int) -> list[str]:
            return ["created", *COLUMNS]

        def col_values(self, column: int) -> list[str]:
            assert column == len(COLUMNS) + 1  # job_id, last
            return ["job_id", "j0", "j1", "j2", "j1"]

        def batch_update(self, data, value_input_option=None) -> None:
            self.updates.append((data, value_input_option))

    worksheet = Worksheet()
    book = types.SimpleNamespace(worksheet=lambda title: worksheet)
    opened: list = []

    class Client:
        def __init__(self, credentials) -> None:
            self.token = credentials.token

        def open_by_key(self, key: str):
            opened.append((self.token, key))
            return book

    gspread = types.ModuleType("gspread")
    gspread.Client = Client
    credentials = types.ModuleType("google.oauth2.credentials")
    credentials.Credentials = lambda token: types.SimpleNamespace(token=token)
    modules = {
        "gspread": gspread,
        "google": types.ModuleType("google"),
        "google.oauth2": types.ModuleType("google.oauth2"),
        "google.oauth2.credentials": credentials,
    }
    with patch.dict(sys.modules, modules):
        assert update_rows("tok", "SHEET", "Prints", "j1", {"result": "failed", "result_by": "user"}) == 2
        assert update_rows("tok", "SHEET", "Prints", "nope", {"result": "failed"}) == 0
    assert opened[0] == ("tok", "SHEET")
    data, mode = worksheet.updates[0]
    assert mode == "RAW"
    # result is column D, result_by column E (after "created", "printer", "file"); rows 3 and 5
    assert data == [
        {"range": "D3", "values": [["failed"]]},
        {"range": "E3", "values": [["user"]]},
        {"range": "D5", "values": [["failed"]]},
        {"range": "E5", "values": [["user"]]},
    ]
    assert len(worksheet.updates) == 1
