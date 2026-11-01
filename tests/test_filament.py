"""Filaments of a multi-material unit: the real Centauri CANVAS, Happy Hare, Spoolman, the Bambu AMS."""

import json
from pathlib import Path

from custom_components.fdm_printer_manager.adapters.bambu_lan import light_command, light_on, merge_report
from custom_components.fdm_printer_manager.filament import (
    FilamentSlot,
    afc_slots,
    bambu_slots,
    klipper_slots,
    mmu_slots,
    norm_color,
    spoolman_slot,
    summary,
)

CENTAURI_STATUS = json.loads((Path(__file__).parent / "fixtures" / "centauri_cosmos_status.json").read_text("utf-8"))


def test_centauri_canvas_lanes() -> None:
    slots = afc_slots(CENTAURI_STATUS)
    assert [s.name for s in slots] == ["CANVAS_1", "CANVAS_2", "CANVAS_3", "CANVAS_4"]
    first = slots[0]
    assert first.material == "PETG"
    assert first.color == "#FFFFFF"
    assert first.remaining == 79.2  # 792 g of 1000 g
    assert first.tool == "T0"
    assert not first.active
    assert not first.empty
    assert all(s.empty and s.material is None for s in slots[1:])
    assert [s.tool for s in slots] == ["T0", "T2", "T1", "T3"]
    assert summary(slots) == "1/4 loaded"


def test_afc_active_lane() -> None:
    status = json.loads(json.dumps(CENTAURI_STATUS))
    status["AFC"]["current_lane"] = "CANVAS_1"
    slots = afc_slots(status)
    assert slots[0].active
    assert summary(slots) == "PETG"


def test_happy_hare_gates() -> None:
    status = {
        "mmu": {
            "gate": 1,
            "gate_material": ["PLA", "PETG", ""],
            "gate_color": ["ff0000", "00FF00", ""],
            "gate_status": [1, 2, 0],
        }
    }
    slots = mmu_slots(status)
    assert [(s.name, s.material, s.color, s.active, s.empty) for s in slots] == [
        ("Gate 0", "PLA", "#FF0000", False, False),
        ("Gate 1", "PETG", "#00FF00", True, False),
        ("Gate 2", None, None, False, True),
    ]
    assert summary(slots) == "PETG"


def test_spoolman_only_without_a_unit() -> None:
    spool = {
        "id": 7,
        "remaining_weight": 250,
        "initial_weight": 1000,
        "filament": {"name": "Galaxy black", "material": "PLA", "color_hex": "1a1a1a"},
    }
    assert spoolman_slot(spool) == [
        FilamentSlot(index=0, name="Galaxy black", material="PLA", color="#1A1A1A", remaining=25.0, active=True)
    ]
    assert spoolman_slot(None) == []
    # A unit wins over Spoolman
    assert klipper_slots(CENTAURI_STATUS, spool)[0].name == "CANVAS_1"
    assert klipper_slots({}, spool)[0].name == "Galaxy black"
    assert klipper_slots({}, None) == []


def test_bambu_ams_and_external_spool() -> None:
    report = {
        "ams": {
            "tray_now": "1",
            "ams": [
                {
                    "id": "0",
                    "tray": [
                        {"id": "0", "tray_type": "PLA", "tray_color": "FF0000FF", "remain": 80},
                        {"id": "1", "tray_type": "PETG", "tray_color": "000000FF", "remain": -1},
                        {"id": "2"},
                        {"id": "3", "tray_type": "", "tray_color": "00000000"},
                    ],
                }
            ],
        },
        "vt_tray": {"id": "254", "tray_type": "TPU", "tray_color": "FFFFFFFF", "remain": 0},
    }
    slots = bambu_slots(report)
    assert [(s.index, s.name, s.material, s.color, s.remaining, s.active, s.empty) for s in slots] == [
        (0, "AMS 1 · 1", "PLA", "#FF0000", 80.0, False, False),
        (1, "AMS 1 · 2", "PETG", "#000000", None, True, False),  # -1: the AMS does not know
        (2, "AMS 1 · 3", None, None, None, False, True),
        (3, "AMS 1 · 4", None, None, None, False, True),
        (254, "External", "TPU", "#FFFFFF", 0.0, False, False),
    ]
    assert summary(slots) == "PETG"


def test_bambu_without_ams_shows_nothing() -> None:
    assert bambu_slots({}) == []
    assert bambu_slots({"ams": {"ams": []}, "vt_tray": {"tray_type": "PLA"}}) == []


def test_partial_ams_report_keeps_the_trays() -> None:
    merged: dict = {}
    merge_report(merged, {"ams": {"tray_now": "255", "ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PLA"}]}]}})
    merge_report(merged, {"ams": {"tray_now": "0"}, "mc_percent": 5})  # only what changed
    assert merged["ams"]["tray_now"] == "0"
    assert bambu_slots(merged)[0].active


def test_bambu_chamber_light() -> None:
    assert light_on({}) is None  # not reported: no light offered
    assert light_on({"lights_report": [{"node": "chamber_light", "mode": "on"}]}) is True
    assert light_on({"lights_report": [{"node": "chamber_light", "mode": "off"}]}) is False
    payload = json.loads(light_command(False))
    assert payload["system"]["command"] == "ledctrl"
    assert payload["system"]["led_node"] == "chamber_light"
    assert payload["system"]["led_mode"] == "off"


def test_norm_color() -> None:
    assert norm_color("#ffffff") == "#FFFFFF"
    assert norm_color("FF000080") == "#FF0000"
    assert norm_color("Red") == "red"
    assert norm_color("") is None
    assert norm_color("#12") is None
