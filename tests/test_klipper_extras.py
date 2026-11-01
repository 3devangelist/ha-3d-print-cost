"""Lights and baby steps, from what the real Ender 6 and Centauri Carbon report (tests/fixtures)."""

import json
from pathlib import Path

import pytest

from custom_components.fdm_printer_manager.adapters.klipper_extras import (
    BABYSTEP_RESET,
    KlipperLight,
    babystep_gcode,
    discover,
    find_lights,
    light_color,
    light_gcode,
    light_level,
    save_gcode,
    z_offset,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


HELP = _load("gcode_help.json")
ENDER_OBJECTS = _load("ender6_objects.json")["objects"]
CENTAURI_OBJECTS = _load("centauri_cosmos_objects.json")["objects"]
ENDER_STATUS = _load("ender6_status.json")
CENTAURI_STATUS = _load("centauri_cosmos_status.json")


def test_ender6_has_its_case_light_pin() -> None:
    features = discover(ENDER_OBJECTS, HELP["ender6"]["commands"], HELP["ender6"]["components"])
    assert features.lights == (KlipperLight("output_pin Luz_interior", "pin", "Luz_interior"),)
    assert features.lights[0].key == "light_luz_interior"
    assert features.lights[0].label == "Luz interior"
    assert features.babystep is True
    assert features.save_gcode == "Z_OFFSET_APPLY_PROBE"  # it has a probe
    assert not features.has_filament_system
    assert features.spoolman is False
    assert features.extra_objects == ("output_pin Luz_interior", "gcode_move")


def test_centauri_has_two_leds_and_four_canvas_lanes() -> None:
    features = discover(CENTAURI_OBJECTS, HELP["centauri"]["commands"], HELP["centauri"]["components"])
    assert [(light.name, light.kind) for light in features.lights] == [("case", "led"), ("hotend", "led")]
    assert features.babystep is True
    assert features.save_gcode == "Z_OFFSET_APPLY_PROBE"  # load_cell_probe
    assert features.afc_lanes == ("CANVAS_1", "CANVAS_2", "CANVAS_3", "CANVAS_4")
    assert "AFC" in features.extra_objects
    assert "AFC_lane CANVAS_4" in features.extra_objects


def test_lights_need_the_command() -> None:
    # Without SET_LED / SET_PIN nothing is offered (the light could not be switched).
    assert find_lights(CENTAURI_OBJECTS, ["SET_PIN"]) == []
    assert find_lights(ENDER_OBJECTS, ["SET_LED"]) == []


def test_output_pin_is_a_light_only_by_name() -> None:
    lights = find_lights(["output_pin caselight", "output_pin beeper", "output_pin chamber_lamp"], ["SET_PIN"])
    assert [light.name for light in lights] == ["caselight", "chamber_lamp"]


def test_no_help_means_no_baby_steps() -> None:
    features = discover(ENDER_OBJECTS, [])
    assert features.babystep is False
    assert features.lights == ()
    assert "gcode_move" not in features.extra_objects


def test_light_state_from_real_status() -> None:
    pin = KlipperLight("output_pin Luz_interior", "pin", "Luz_interior")
    case = KlipperLight("led case", "led", "case")
    hotend = KlipperLight("led hotend", "led", "hotend")
    assert light_level(pin, ENDER_STATUS) == 0.5
    assert light_level(case, CENTAURI_STATUS) == 1.0  # white channel on
    assert light_level(hotend, CENTAURI_STATUS) == 0.0
    assert light_level(case, {}) is None
    assert light_color(case, CENTAURI_STATUS) == (0.0, 0.0, 0.0, 1.0)
    assert light_color(pin, ENDER_STATUS) == (0.5,)


def test_light_gcode() -> None:
    pin = KlipperLight("output_pin Luz_interior", "pin", "Luz_interior")
    case = KlipperLight("led case", "led", "case")
    assert light_gcode(pin, False) == "SET_PIN PIN=Luz_interior VALUE=0"
    assert light_gcode(pin, True) == "SET_PIN PIN=Luz_interior VALUE=1"
    assert light_gcode(pin, True, (0.5,)) == "SET_PIN PIN=Luz_interior VALUE=0.5"  # back as it was
    assert light_gcode(case, False) == "SET_LED LED=case RED=0 GREEN=0 BLUE=0 WHITE=0"
    assert light_gcode(case, True) == "SET_LED LED=case RED=1 GREEN=1 BLUE=1 WHITE=1"
    assert light_gcode(case, True, (0.0, 0.0, 0.0, 1.0)) == "SET_LED LED=case RED=0 GREEN=0 BLUE=0 WHITE=1"


def test_z_offset() -> None:
    assert z_offset(ENDER_STATUS) == 0.0
    assert z_offset({"gcode_move": {"homing_origin": [0, 0, -0.0251, 0]}}) == -0.0251
    assert z_offset({}) is None


@pytest.mark.parametrize(("delta", "expected"), [(0.01, "+0.010"), (-0.005, "-0.005"), (0.05, "+0.050")])
def test_babystep_gcode(delta: float, expected: str) -> None:
    assert babystep_gcode(delta, 0.0) == f"SET_GCODE_OFFSET Z_ADJUST={expected} MOVE=1"


def test_babystep_limits() -> None:
    with pytest.raises(ValueError, match="one of"):
        babystep_gcode(0.2, 0.0)  # not one of the steps
    with pytest.raises(ValueError, match="1 mm"):
        babystep_gcode(0.05, 0.98)  # past +1 mm
    with pytest.raises(ValueError, match="1 mm"):
        babystep_gcode(-0.05, -0.99)
    assert babystep_gcode(-0.05, 0.98)  # back towards 0 is fine
    assert BABYSTEP_RESET == "SET_GCODE_OFFSET Z=0 MOVE=1"


def test_save_gcode() -> None:
    assert save_gcode(["probe"], ["Z_OFFSET_APPLY_PROBE", "Z_OFFSET_APPLY_ENDSTOP"]) == "Z_OFFSET_APPLY_PROBE"
    assert save_gcode(["bltouch"], ["Z_OFFSET_APPLY_PROBE"]) == "Z_OFFSET_APPLY_PROBE"
    assert save_gcode(["extruder"], ["Z_OFFSET_APPLY_ENDSTOP"]) == "Z_OFFSET_APPLY_ENDSTOP"
    assert save_gcode(["extruder"], []) is None
