"""0.15.0: the end of a print on the phone with "Keep on", the wait before the auto-off, and "Power off now"."""

from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.fdm_printer_manager.adapters.moonraker import KlipperStatus
from custom_components.fdm_printer_manager.const import DOMAIN, EVENT_PRINT_FINISHED, MOBILE_ACTION_EVENT
from custom_components.fdm_printer_manager.finish_notify import duration_text, finish_message, finish_targets

from .test_auto_off import Machine, attrs, computer_goes_off, finish_a_print, machine, setup  # noqa: F401
from .test_controls import CLIENT, PLUG, phase, tick

PHONE = "mobile_app_phone"
KEEP_ON = "button.ender_6_keep_on"
FORCE = "button.ender_6_power_off_now"


@pytest.fixture(autouse=True)
def no_history():
    """Moonraker's print history is empty (the end of a print reads it for the filament)."""
    with patch(f"{CLIENT}.history", AsyncMock(return_value=[])):
        yield


def phone(hass: HomeAssistant, name: str = PHONE) -> list[ServiceCall]:
    return async_mock_service(hass, "notify", name)


async def press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
    await hass.async_block_till_done()


# --------------------------------------------------------------------------- #
# The message
# --------------------------------------------------------------------------- #
def test_finish_message_says_when_it_turns_off() -> None:
    from datetime import datetime  # noqa: PLC0415

    title, text = finish_message(
        "Ender 6",
        "vespa.gcode",
        "success",
        7500,
        off_at=datetime(2026, 11, 8, 14, 35),
        off_pending=True,
        cool_to=80,
        why_on=None,
    )
    assert title == "✅ Ender 6: print finished"
    assert text.splitlines() == [
        "vespa.gcode · success · 2 h 05 min",
        "Turns off by itself at 14:35 (nozzle below 80 °C first). Keep on: leave it on.",
    ]
    title, text = finish_message(
        "Ender 6", "a.gcode", "failed", 600, off_at=None, off_pending=False, cool_to=80, why_on="Stays on."
    )
    assert title == "❌ Ender 6: print failed"
    assert text.endswith("Stays on.")
    assert duration_text(2100) == "35 min"


def test_who_gets_it() -> None:
    shared = {"finish_notify_services": "notify.mobile_app_tablet"}
    assert finish_targets({"finish_notify_services": "notify.mobile_app_a, mobile_app_b"}, shared) == [
        "mobile_app_a",
        "mobile_app_b",
    ]
    assert finish_targets({}, shared) == ["mobile_app_tablet"]  # the printer has none: FDM Printers
    assert finish_targets({"finish_notify": False}, shared) == []  # turned off
    assert finish_targets({}, {}) == []  # nobody chosen: nothing is sent


# --------------------------------------------------------------------------- #
# Notification + Keep on
# --------------------------------------------------------------------------- #
async def test_notification_with_keep_on_and_the_wait(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    machine: Machine,  # noqa: F811
) -> None:
    freezer.move_to("2026-11-08 10:00:00+00:00")
    sent = phone(hass)
    entry = await setup(hass, auto_off_delay=10, finish_notify_services=f"notify.{PHONE}")
    await finish_a_print(hass, freezer, machine, "complete")
    await hass.async_block_till_done()

    assert phase(hass) == "waiting"
    assert attrs(hass)["auto_off_delay"] == 10
    assert attrs(hass)["waiting_until"] == "2026-11-08T10:10:10+00:00"
    assert machine.calls == []  # nothing is shut down while it waits
    assert len(sent) == 1
    data = sent[0].data
    assert data["title"] == "✅ Ender 6: print finished"
    assert "a.gcode · success" in data["message"]
    assert "Turns off by itself at" in data["message"]
    assert data["data"]["actions"] == [{"action": f"FDM_KEEP_ON_{entry.entry_id}", "title": "Keep on"}]

    # Another printer's button does nothing here.
    hass.bus.async_fire(MOBILE_ACTION_EVENT, {"action": "FDM_KEEP_ON_another"})
    await hass.async_block_till_done()
    assert phase(hass) == "waiting"

    # "Keep on" tapped on the phone: the auto-off is cancelled.
    hass.bus.async_fire(MOBILE_ACTION_EVENT, {"action": f"FDM_KEEP_ON_{entry.entry_id}"})
    await hass.async_block_till_done()
    assert phase(hass) == "on"
    assert attrs(hass)["last_result"] == "kept_on"
    await tick(hass, freezer, 30 * 60)
    assert machine.calls == []

    # The next print turns it off again as usual (after the wait).
    await finish_a_print(hass, freezer, machine, "complete")
    assert phase(hass) == "waiting"
    await tick(hass, freezer, 10 * 60 + 5)
    await tick(hass, freezer, 5)
    assert machine.calls == ["shutdown"]
    assert phase(hass) == "shutting_down"


async def test_after_the_wait_it_turns_off_safely(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    machine: Machine,  # noqa: F811
) -> None:
    freezer.move_to("2026-11-08 10:00:00+00:00")
    cut = async_mock_service(hass, "homeassistant", "turn_off")
    await setup(hass, auto_off_delay=10)
    await finish_a_print(hass, freezer, machine, "complete")
    await tick(hass, freezer, 9 * 60)
    assert phase(hass) == "waiting"
    assert machine.calls == []
    await tick(hass, freezer, 70)
    assert machine.calls == ["shutdown"]
    await computer_goes_off(hass, freezer, machine)
    assert len(cut) == 1


async def test_a_new_print_while_waiting_cancels_it(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    machine: Machine,  # noqa: F811
) -> None:
    freezer.move_to("2026-11-08 10:00:00+00:00")
    await setup(hass, auto_off_delay=10)
    await finish_a_print(hass, freezer, machine, "complete")
    assert phase(hass) == "waiting"
    machine.reading = KlipperStatus(state="printing", filename="b.gcode", progress=1, nozzle_temp=210.0)
    await tick(hass, freezer, 5)
    assert phase(hass) == "on"
    await tick(hass, freezer, 20 * 60)
    assert machine.calls == []


async def test_keep_on_button_and_during_a_print(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    machine: Machine,  # noqa: F811
) -> None:
    freezer.move_to("2026-11-08 10:00:00+00:00")
    sent = phone(hass)
    await setup(hass, finish_notify_services=PHONE)
    # Pressed during the print: this print does not turn the printer off.
    machine.reading = KlipperStatus(state="printing", filename="a.gcode", progress=50, nozzle_temp=210.0)
    await tick(hass, freezer, 5)
    await press(hass, KEEP_ON)
    assert attrs(hass)["keep_on"] is True
    machine.reading = KlipperStatus(state="complete", filename="a.gcode", progress=100, nozzle_temp=60.0)
    await tick(hass, freezer, 5)
    await hass.async_block_till_done()
    assert phase(hass) == "on"
    assert machine.calls == []
    assert attrs(hass)["keep_on"] is False
    assert attrs(hass)["last_result"] == "kept_on"
    assert "Stays on: Keep on was pressed during the print." in sent[0].data["message"]
    assert "actions" not in sent[0].data["data"]

    # Pressed while the nozzle cools after the next print: cancelled too. Without a wait it cools first.
    await finish_a_print(hass, freezer, machine, "complete", nozzle=150.0)
    assert phase(hass) == "cooling"
    result = await hass.services.async_call(
        DOMAIN,
        "keep_on",
        {"entry_id": hass.config_entries.async_entries(DOMAIN)[0].entry_id},
        blocking=True,
        return_response=True,
    )
    assert result == {"done": "cancelled"}
    assert phase(hass) == "on"
    await tick(hass, freezer, 3600)
    assert machine.calls == []


async def test_failed_print_notification_and_nobody_chosen(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    machine: Machine,  # noqa: F811
) -> None:
    freezer.move_to("2026-11-08 10:00:00+00:00")
    sent = phone(hass)
    entry = await setup(hass, finish_notify_services=PHONE)
    await finish_a_print(hass, freezer, machine, "error")
    await hass.async_block_till_done()
    assert sent[0].data["title"] == "❌ Ender 6: print failed"
    assert "Stays on so you can look at it." in sent[0].data["message"]
    assert "actions" not in sent[0].data["data"]

    # Turned off for this printer: nothing more.
    hass.config_entries.async_update_entry(entry, options={**entry.options, "finish_notify": False})
    await hass.async_block_till_done()
    await finish_a_print(hass, freezer, machine, "complete")
    await hass.async_block_till_done()
    assert len(sent) == 1


async def test_fdm_printers_phone_is_used_when_the_printer_has_none(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    machine: Machine,  # noqa: F811
) -> None:
    freezer.move_to("2026-11-08 10:00:00+00:00")
    panel = MockConfigEntry(domain=DOMAIN, title="FDM Printers", unique_id="panel", data={"panel": True})
    panel.add_to_hass(hass)
    hass.config_entries.async_update_entry(panel, options={"finish_notify_services": "notify.mobile_app_tablet"})
    tablet = phone(hass, "mobile_app_tablet")
    await setup(hass)
    await finish_a_print(hass, freezer, machine, "cancelled")
    await hass.async_block_till_done()
    assert tablet[0].data["title"] == "⏹️ Ender 6: print cancelled"


# --------------------------------------------------------------------------- #
# Power off now
# --------------------------------------------------------------------------- #
async def test_power_off_now_needs_three_presses(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    machine: Machine,  # noqa: F811
) -> None:
    freezer.move_to("2026-11-08 10:00:00+00:00")
    cut = async_mock_service(hass, "homeassistant", "turn_off")
    await setup(hass)
    await press(hass, FORCE)
    await press(hass, FORCE)
    assert hass.states.get(FORCE).attributes["presses"] == 2
    assert cut == []
    # More than 5 s: the count starts again.
    freezer.tick(6)
    await press(hass, FORCE)
    assert hass.states.get(FORCE).attributes["presses"] == 1
    await press(hass, FORCE)
    assert cut == []
    await press(hass, FORCE)
    assert len(cut) == 1
    assert cut[0].data["entity_id"] == PLUG
    assert machine.calls == []  # no shutdown asked, no SSH: the plug is cut at once
    assert machine.ssh.await_count == 0
    assert attrs(hass)["last_result"] == "forced"
    assert hass.states.get(FORCE).attributes["presses"] == 0


async def test_power_off_now_service_needs_confirm(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    machine: Machine,  # noqa: F811
) -> None:
    freezer.move_to("2026-11-08 10:00:00+00:00")
    cut = async_mock_service(hass, "homeassistant", "turn_off")
    entry = await setup(hass)
    for data in ({"entry_id": entry.entry_id}, {"entry_id": entry.entry_id, "confirm": False}):
        with pytest.raises((vol.Invalid, ServiceValidationError)):
            await hass.services.async_call(DOMAIN, "force_power_off", data, blocking=True)
    with pytest.raises(HomeAssistantError, match="confirm"):
        await entry.runtime_data.async_force_power_off(confirm=False)
    assert cut == []
    await hass.services.async_call(
        DOMAIN, "force_power_off", {"entry_id": entry.entry_id, "confirm": True}, blocking=True
    )
    assert len(cut) == 1


async def test_power_off_now_during_a_print_records_it_as_failed(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    machine: Machine,  # noqa: F811
) -> None:
    freezer.move_to("2026-11-08 10:00:00+00:00")
    events = []
    hass.bus.async_listen(EVENT_PRINT_FINISHED, events.append)
    sent = phone(hass)
    cut = async_mock_service(hass, "homeassistant", "turn_off")
    entry = await setup(hass, finish_notify_services=PHONE)
    machine.reading = KlipperStatus(state="printing", filename="a.gcode", progress=40, nozzle_temp=210.0)
    await tick(hass, freezer, 5)
    await press(hass, KEEP_ON)  # also forgotten by the cut

    await hass.services.async_call(
        DOMAIN, "force_power_off", {"entry_id": entry.entry_id, "confirm": True}, blocking=True
    )
    hass.states.async_set(PLUG, "off")
    await hass.async_block_till_done(wait_background_tasks=True)  # the print is stored (nothing else runs)
    assert len(cut) == 1
    assert machine.calls == []
    assert len(events) == 1
    data = events[0].data
    assert data["result"] == "failed"
    assert data["result_by"] == "user"
    assert "Stopped by the user: the plug was cut with Power off now (no cooling, no shutdown)" in data["notes"]
    coordinator = entry.runtime_data
    assert coordinator.state.history[0]["result"] == "failed"
    assert coordinator.tracker.active_job is None
    assert sent == []  # the user did it: no "print failed" notification
    assert attrs(hass)["keep_on"] is False

    # Moonraker still answers "printing" for a moment: that is not a new print.
    await tick(hass, freezer, 5)
    await tick(hass, freezer, 5)
    assert coordinator.tracker.active_job is None
    assert phase(hass) == "off"
    assert machine.calls == []


async def test_power_off_now_cancels_the_auto_off(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    machine: Machine,  # noqa: F811
) -> None:
    freezer.move_to("2026-11-08 10:00:00+00:00")
    cut = async_mock_service(hass, "homeassistant", "turn_off")
    entry = await setup(hass, auto_off_delay=10)
    await finish_a_print(hass, freezer, machine, "complete")
    assert phase(hass) == "waiting"
    await entry.runtime_data.async_force_power_off(confirm=True)
    hass.states.async_set(PLUG, "off")
    await hass.async_block_till_done()
    assert len(cut) == 1
    assert phase(hass) == "off"
    await tick(hass, freezer, 20 * 60)
    assert machine.calls == []
    assert len(cut) == 1
