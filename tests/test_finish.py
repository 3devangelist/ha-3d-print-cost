"""When a print ends: a to-do item and the user's script."""

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, ServiceCall

from custom_components.fdm_printer_manager.adapters.moonraker import KlipperStatus
from custom_components.fdm_printer_manager.finish import todo_item

from .test_klipper_integration import FILE, poll, printer, setup_ender  # noqa: F401 - printer is a fixture


def test_todo_item_text() -> None:
    assert todo_item({"printer": "Ender 6", "name": "cover.gcode", "result": "success"}) == (
        "Remove the print from Ender 6",
        "cover.gcode — finished",
    )
    assert todo_item({}) == ("Remove the print from the printer", "print — ended")


async def test_print_end_adds_todo_and_runs_script(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    printer,  # noqa: F811
) -> None:
    todo_calls: list[ServiceCall] = []
    script_calls: list[ServiceCall] = []
    hass.services.async_register("todo", "add_item", todo_calls.append)
    hass.services.async_register("script", "turn_on", script_calls.append)
    freezer.move_to("2026-10-28 10:00:00+00:00")
    await setup_ender(hass, todo_entity="todo.workshop", end_script="script.print_done")

    printer.reading = KlipperStatus(state="printing", filename=FILE, progress=0.0, print_duration_s=0)
    await poll(hass, freezer, 0.1, 10000)
    printer.reading = KlipperStatus(state="complete", filename=FILE, progress=100.0, print_duration_s=3600)
    await poll(hass, freezer, 60, 10500)

    assert len(todo_calls) == 1
    assert todo_calls[0].data["item"] == "Remove the print from Ender 6"
    assert todo_calls[0].data["description"] == f"{FILE} — finished"
    assert todo_calls[0].data["entity_id"] == "todo.workshop"
    assert len(script_calls) == 1
    variables = script_calls[0].data["variables"]
    assert variables["printer"] == "Ender 6"
    assert variables["result"] == "success"


async def test_nothing_configured_nothing_called(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    printer,  # noqa: F811
) -> None:
    todo_calls: list[ServiceCall] = []
    hass.services.async_register("todo", "add_item", todo_calls.append)
    freezer.move_to("2026-10-28 10:00:00+00:00")
    await setup_ender(hass)
    printer.reading = KlipperStatus(state="printing", filename=FILE, progress=0.0, print_duration_s=0)
    await poll(hass, freezer, 0.1, 10000)
    printer.reading = KlipperStatus(state="complete", filename=FILE, progress=100.0, print_duration_s=3600)
    await poll(hass, freezer, 60, 10500)
    assert todo_calls == []
