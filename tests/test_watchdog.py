"""Watching a running print: printer down, interrupted or stuck print, no progress."""

from datetime import UTC, datetime, timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import Event, HomeAssistant, ServiceCall

from custom_components.fdm_printer_manager.adapters.moonraker import KlipperStatus, MoonrakerConnectionError
from custom_components.fdm_printer_manager.alerts import parse_notify_services
from custom_components.fdm_printer_manager.const import EVENT_ALERT
from custom_components.fdm_printer_manager.watchdog import AlertKind, Watchdog, WatchInput

from .test_integration import make_entry, set_states, step
from .test_klipper_integration import FILE, poll, printer, setup_ender  # noqa: F401 - printer is a fixture

T0 = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)


def at(minutes: float, **kw) -> WatchInput:
    base = {"job_open": True, "reachable": True, "printing": True, "progress": 10.0, "status": "printing"}
    return WatchInput(now=T0 + timedelta(minutes=minutes), **{**base, **kw})


def kinds(actions) -> list[str]:
    return [str(a.kind) for a in actions]


def test_down_then_resumed() -> None:
    dog = Watchdog()
    assert dog.check(at(0)) == []
    assert dog.check(at(1, reachable=False)) == []
    actions = dog.check(at(3.5, reachable=False))
    assert kinds(actions) == ["down"]
    assert dog.alert is AlertKind.DOWN
    assert dog.check(at(4, reachable=False)) == []  # said once
    actions = dog.check(at(6, progress=11.0))
    assert kinds(actions) == ["resumed"]
    assert dog.alert is None


def test_back_without_resuming_is_closed_as_failed_after_power_loss() -> None:
    dog = Watchdog(close_after_s=600)
    dog.check(at(0))
    dog.check(at(1, reachable=False))
    dog.check(at(4, reachable=False))
    assert kinds(dog.check(at(5, printing=False, status="standby"))) == ["back"]
    assert dog.check(at(14, printing=False, status="standby")) == []
    actions = dog.check(at(15, printing=False, status="standby"))
    assert kinds(actions) == ["interrupted"]
    assert actions[0].close_job
    assert actions[0].after_power_loss


def test_stuck_state_without_power_loss() -> None:
    dog = Watchdog(close_after_s=600)
    dog.check(at(0, printing=False, status="standby"))
    actions = dog.check(at(10, printing=False, status="standby"))
    assert actions[0].kind is AlertKind.INTERRUPTED
    assert not actions[0].after_power_loss


def test_no_progress_warning_once() -> None:
    dog = Watchdog(stall_after_s=3600)
    dog.check(at(0, progress=40.0))
    assert dog.check(at(59, progress=40.0)) == []
    assert kinds(dog.check(at(61, progress=40.0))) == ["stalled"]
    assert dog.check(at(90, progress=40.0)) == []
    assert dog.alert is AlertKind.STALLED
    dog.check(at(91, progress=41.0))
    assert dog.alert is None


def test_nothing_without_a_print() -> None:
    dog = Watchdog()
    assert dog.check(at(0, job_open=False, reachable=False)) == []
    assert dog.check(at(30, job_open=False, reachable=False)) == []


def test_parse_notify_services() -> None:
    assert parse_notify_services("notify.mobile_app_a, mobile_app_b;notify.c ") == ["mobile_app_a", "mobile_app_b", "c"]
    assert parse_notify_services(None) == []


@pytest.fixture
def alerts(hass: HomeAssistant) -> list[Event]:
    captured: list[Event] = []
    hass.bus.async_listen(EVENT_ALERT, captured.append)
    return captured


async def test_klipper_power_cut_closes_the_print_as_failed(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    printer,  # noqa: F811
    alerts,
) -> None:
    sent: list[ServiceCall] = []
    hass.services.async_register("notify", "mobile_app_phone", sent.append)
    freezer.move_to("2026-10-28 10:00:00+00:00")
    entry = await setup_ender(hass, notify_services="notify.mobile_app_phone")
    printer.reading = KlipperStatus(state="printing", filename=FILE, progress=30.0, print_duration_s=600)
    await poll(hass, freezer, 0.1, 10000)

    printer.reading = MoonrakerConnectionError("timeout")  # power cut
    await poll(hass, freezer, 1, 10050)
    await poll(hass, freezer, 2, 10050)
    assert [e.data["kind"] for e in alerts] == ["down"]
    assert hass.states.get("sensor.ender_6_printer_status").attributes["alert"] == "down"
    assert len(sent) == 1
    assert "Ender 6" in sent[0].data["title"]

    # Power back: Klipper starts in "standby", the print is not resumed.
    printer.reading = KlipperStatus(state="standby")
    await poll(hass, freezer, 1, 10050)
    assert [e.data["kind"] for e in alerts] == ["down", "back"]
    assert hass.states.get("sensor.ender_6_current_print_phase").state == "active"
    await poll(hass, freezer, 10, 10060)

    assert [e.data["kind"] for e in alerts] == ["down", "back", "interrupted"]
    assert hass.states.get("sensor.ender_6_last_print_result").state == "failed"
    last = hass.states.get("sensor.ender_6_last_print_cost").attributes
    assert last["result_by"] == "watchdog"
    assert any("lost power" in n for n in last["notes"])
    assert hass.states.get("sensor.ender_6_current_print_phase").state == "idle"
    assert entry.runtime_data.state.history[0]["result"] == "failed"


async def test_klipper_resumed_print_keeps_going(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    printer,  # noqa: F811
    alerts,
) -> None:
    freezer.move_to("2026-10-28 10:00:00+00:00")
    await setup_ender(hass)
    printer.reading = KlipperStatus(state="printing", filename=FILE, progress=30.0, print_duration_s=600)
    await poll(hass, freezer, 0.1, 10000)
    printer.reading = MoonrakerConnectionError("wifi")
    await poll(hass, freezer, 1, 10050)
    await poll(hass, freezer, 2, 10050)
    printer.reading = KlipperStatus(state="printing", filename=FILE, progress=31.0, print_duration_s=800)
    await poll(hass, freezer, 1, 10060)
    assert [e.data["kind"] for e in alerts] == ["down", "resumed"]
    assert hass.states.get("sensor.ender_6_current_print_phase").state == "active"


async def test_alerts_can_be_turned_off(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    printer,  # noqa: F811
    alerts,
) -> None:
    freezer.move_to("2026-10-28 10:00:00+00:00")
    await setup_ender(hass, alerts=False)
    printer.reading = KlipperStatus(state="printing", filename=FILE, progress=30.0, print_duration_s=600)
    await poll(hass, freezer, 0.1, 10000)
    printer.reading = KlipperStatus(state="standby")
    await poll(hass, freezer, 30, 10050)
    assert alerts == []
    assert hass.states.get("sensor.ender_6_current_print_phase").state == "active"


async def test_bambu_power_cut_ends_as_failed(hass: HomeAssistant, freezer: FrozenDateTimeFactory, alerts) -> None:
    """ha-bambulab: the status goes unavailable, then the printer comes back "idle"."""
    freezer.move_to("2026-10-23 14:00:00+00:00")
    set_states(hass, status="idle", energy="100000")
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await step(hass, freezer, 0, status="running", energy="100000")
    await step(hass, freezer, 1, status="unavailable", energy="100010")
    await step(hass, freezer, 3, status="unavailable", energy="100010")
    assert [e.data["kind"] for e in alerts] == ["down"]
    await step(hass, freezer, 2, status="idle", energy="100010")
    assert hass.states.get("sensor.x2d_last_print_result").state == "failed"
    assert hass.states.get("sensor.x2d_last_print_cost").attributes["result_by"] == "watchdog"
    assert [e.data["kind"] for e in alerts] == ["down", "interrupted"]
