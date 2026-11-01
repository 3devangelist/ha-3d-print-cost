"""Marking a finished print as a success or a failure by hand, and the history numbers."""

from unittest.mock import MagicMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fdm_printer_manager import ws_history
from custom_components.fdm_printer_manager.adapters.moonraker import KlipperStatus
from custom_components.fdm_printer_manager.const import DOMAIN, EVENT_PRINT_MARKED
from custom_components.fdm_printer_manager.outcome import history_stats, mark_history
from custom_components.fdm_printer_manager.storage import JobStore

from .test_klipper_integration import FILE, poll, printer, setup_ender  # noqa: F401 - printer is a fixture


def test_history_stats_success_rate_and_wasted_cost() -> None:
    rows = [
        {"result": "success", "electricity_cost": 0.10, "energy_kwh": 0.5},
        {"result": "success", "electricity_cost": 0.20, "energy_kwh": 1.0},
        {"result": "failed", "electricity_cost": 0.05, "energy_kwh": 0.2},
        {"result": "cancelled", "electricity_cost": 0.01, "energy_kwh": 0.05},
        {"result": None, "electricity_cost": None},
    ]
    stats = history_stats(rows)
    assert stats["prints"] == 5
    assert stats["counts"] == {"success": 2, "cancelled": 1, "failed": 1, "unknown": 1}
    assert stats["success_rate"] == pytest.approx(66.7)
    assert stats["wasted_cost"] == pytest.approx(0.06)
    assert history_stats([])["success_rate"] is None


def test_mark_history_changes_only_that_print() -> None:
    rows = [{"job_id": "a", "result": "success"}, {"job_id": "b", "result": "success"}]
    assert mark_history(rows, "b", "failed")
    assert rows[0] == {"job_id": "a", "result": "success"}
    assert rows[1] == {"job_id": "b", "result": "failed", "result_by": "user"}
    assert not mark_history(rows, "zzz", "failed")


async def _finish_one_print(hass, freezer, printer) -> MockConfigEntry:  # noqa: F811
    freezer.move_to("2026-10-28 10:00:00+00:00")
    entry = await setup_ender(hass)
    printer.reading = KlipperStatus(state="printing", filename=FILE, progress=0.0, print_duration_s=0)
    await poll(hass, freezer, 0.1, 10000)
    printer.reading = KlipperStatus(state="complete", filename=FILE, progress=100.0, print_duration_s=3600)
    await poll(hass, freezer, 60, 10500)
    assert hass.states.get("sensor.ender_6_last_print_result").state == "success"
    return entry


async def test_mark_last_print_failed(hass: HomeAssistant, freezer: FrozenDateTimeFactory, printer) -> None:  # noqa: F811
    entry = await _finish_one_print(hass, freezer, printer)
    marked: list[Event] = []
    hass.bus.async_listen(EVENT_PRINT_MARKED, marked.append)

    await hass.services.async_call(
        DOMAIN, "mark_print", {"entry_id": entry.entry_id, "result": "failed"}, blocking=True
    )
    await hass.async_block_till_done()

    state = hass.states.get("sensor.ender_6_last_print_cost")
    assert hass.states.get("sensor.ender_6_last_print_result").state == "failed"
    assert state.attributes["result_by"] == "user"
    assert len(marked) == 1
    assert marked[0].data["result"] == "failed"

    coordinator = entry.runtime_data
    assert coordinator.state.history[0]["result"] == "failed"
    assert coordinator.state.history[0]["result_by"] == "user"

    # Kept across a restart.
    stored = await JobStore(hass, entry.entry_id).async_load()
    assert stored.last_job.result == "failed"
    assert stored.last_job.result_by == "user"
    assert stored.history[0]["result"] == "failed"


async def test_mark_history_row_and_history_stats(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    printer,  # noqa: F811
) -> None:
    entry = await _finish_one_print(hass, freezer, printer)
    job_id = entry.runtime_data.state.history[0]["job_id"]
    await hass.services.async_call(
        DOMAIN, "mark_print", {"entry_id": entry.entry_id, "result": "failed", "job_id": job_id}, blocking=True
    )
    # The websocket command, called directly (a real client's token would expire on the frozen clock).
    connection = MagicMock()
    connection.user.is_admin = True
    ws_history(hass, connection, {"id": 1, "type": f"{DOMAIN}/history", "limit": 50, "offset": 0})
    await hass.async_block_till_done()
    result = connection.send_result.call_args.args[1]
    assert result["rows"][0]["result"] == "failed"
    assert result["stats"]["counts"]["failed"] == 1
    assert result["stats"]["success_rate"] == 0


async def test_mark_print_refuses_bad_input(hass: HomeAssistant, freezer: FrozenDateTimeFactory, printer) -> None:  # noqa: F811
    freezer.move_to("2026-10-28 10:00:00+00:00")
    entry = await setup_ender(hass)
    with pytest.raises(HomeAssistantError, match="no finished print"):
        await hass.services.async_call(
            DOMAIN, "mark_print", {"entry_id": entry.entry_id, "result": "success"}, blocking=True
        )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "mark_print", {"entry_id": "nope", "result": "success"}, blocking=True)
