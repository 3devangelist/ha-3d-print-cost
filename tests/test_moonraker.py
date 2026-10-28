"""Klipper / Moonraker parsing (pure functions, no printer needed)."""

from custom_components.fdm_printer_manager.adapters.moonraker import (
    FileMetadata,
    KlipperStatus,
    default_web_url,
    parse_metadata,
    parse_status,
    remaining_minutes,
)

PRINTING = {
    "print_stats": {"state": "printing", "filename": "gsu_cover_PLA_1h12m.gcode", "print_duration": 1800.0},
    "virtual_sdcard": {"progress": 0.4123},
    "display_status": {"progress": 0.40},
    "extruder": {"temperature": 214.8, "target": 215.0},
}


def test_parse_printing() -> None:
    r = parse_status(PRINTING, "ready")
    assert r.state == "printing"
    assert r.filename == "gsu_cover_PLA_1h12m.gcode"
    assert r.progress == 41.2  # virtual_sdcard wins, as a percentage
    assert r.print_duration_s == 1800.0
    assert r.nozzle_temp == 214.8


def test_parse_falls_back_to_display_status() -> None:
    status = {**PRINTING, "virtual_sdcard": {}}
    assert parse_status(status, "ready").progress == 40.0


def test_parse_terminal_states_pass_through() -> None:
    for state in ("complete", "cancelled", "error", "paused", "standby"):
        assert parse_status({"print_stats": {"state": state}}, "ready").state == state


def test_klipper_shutdown_reads_as_error() -> None:
    # Klipper itself is down (e.g. MCU error during a print): close the job as failed.
    assert parse_status({}, "shutdown").state == "error"
    # Starting up: nothing to conclude, keep whatever job is open.
    assert parse_status({}, "startup").state == "standby"


def test_empty_filename_is_none() -> None:
    assert parse_status({"print_stats": {"state": "standby", "filename": ""}}, "ready").filename is None


def test_metadata_and_remaining_time() -> None:
    meta = parse_metadata({"estimated_time": 4320, "filament_weight_total": 31.7})
    assert meta == FileMetadata(estimated_time_s=4320.0, filament_weight_g=31.7)
    reading = KlipperStatus(state="printing", progress=41.2, print_duration_s=1800.0)
    assert remaining_minutes(reading, meta) == 42.0
    # Without slicer metadata: extrapolate from progress.
    assert round(remaining_minutes(reading, None), 1) == 42.8


def test_default_web_url() -> None:
    assert default_web_url("192.168.1.89") == "http://192.168.1.89/"
    assert default_web_url("http://ender6.local/") == "http://ender6.local/"
    assert default_web_url("fe80::1") == "http://[fe80::1]/"
