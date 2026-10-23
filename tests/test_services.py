"""Service actions: get_history and estimate_cost."""

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.ha_3d_print_cost.adapters.haspelsync import SlotSpool
from custom_components.ha_3d_print_cost.adapters.spoolman import SpoolmanError
from custom_components.ha_3d_print_cost.const import (
    CONF_GCODE_FOLDER,
    CONF_HASPELSYNC_PRINTER_ID,
    CONF_HASPELSYNC_URL,
    CONF_SPOOLMAN_URL,
    DOMAIN,
)
from custom_components.ha_3d_print_cost.models import SpoolInfo

from .helpers import SERIAL
from .test_gcode import BAMBU_GCODE, make_3mf
from .test_integration import make_entry, set_states

SPOOLS = {
    23: SpoolInfo(spool_id=23, spool_price=Decimal(20), initial_weight_g=1000, material="PLA", name="Black"),
    45: SpoolInfo(spool_id=45, spool_price=Decimal(25), initial_weight_g=1000, material="PLA", name="White"),
}
PAST_JOB = {
    "job_id": "j1",
    "name": "Benchy",
    "result": "success",
    "duration_s": 3600,
    "energy_kwh": 0.12,
    "electricity_cost": 0.02,
    "filament_cost": 0.3,
    "total_cost": 0.32,
    "partial_total_cost": 0.32,
}


async def _setup(hass: HomeAssistant, **extra):
    set_states(hass, status="idle", energy="0", price="0.20")
    entry = make_entry(hass, **extra)
    assert await hass.config_entries.async_setup(entry.entry_id)
    # The initial G-code folder scan runs as a background task.
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


async def _call(hass: HomeAssistant, service: str, data: dict) -> dict:
    return await hass.services.async_call(DOMAIN, service, data, blocking=True, return_response=True)


# --------------------------------------------------------------------------- #
# get_history
# --------------------------------------------------------------------------- #
async def test_get_history(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    failed = {**PAST_JOB, "job_id": "j2", "result": "failed", "total_cost": None, "partial_total_cost": 0.01}
    entry.runtime_data.state.history[:] = [failed, PAST_JOB]

    response = await _call(hass, "get_history", {})
    assert response["printer"] == "X2D"
    assert response["currency"] == "EUR"
    assert [j["job_id"] for j in response["jobs"]] == ["j2", "j1"]
    assert response["totals"]["prints"] == 2
    assert response["totals"]["incomplete_jobs"] == 1
    assert response["totals"]["total_cost"] == pytest.approx(0.33)

    response = await _call(hass, "get_history", {"result": "success", "config_entry_id": entry.entry_id})
    assert [j["job_id"] for j in response["jobs"]] == ["j1"]
    response = await _call(hass, "get_history", {"limit": 1})
    assert [j["job_id"] for j in response["jobs"]] == ["j2"]


async def test_unknown_entry(hass: HomeAssistant) -> None:
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, "get_history", {"config_entry_id": "nope"})


# --------------------------------------------------------------------------- #
# estimate_cost
# --------------------------------------------------------------------------- #
@pytest.fixture
def gcode_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    return tmp_path


async def test_estimate_with_ams_match(hass: HomeAssistant, gcode_dir: Path) -> None:
    path = make_3mf(gcode_dir / "lamp.gcode.3mf", plates={1: BAMBU_GCODE})
    slots = {
        "A1": SlotSpool(slot="A1", spool_id=23, material="PLA", color="#000000"),
        "A2": SlotSpool(slot="A2", spool_id=45, material="PLA", color="#FFFFFF"),
    }
    with (
        patch(
            "custom_components.ha_3d_print_cost.coordinator.HaspelsyncClient.get_current_slots",
            AsyncMock(return_value=slots),
        ) as get_slots,
        patch(
            "custom_components.ha_3d_print_cost.coordinator.SpoolmanClient.get_spool",
            side_effect=lambda spool_id: SPOOLS[spool_id],
        ),
    ):
        entry = await _setup(
            hass,
            **{
                CONF_HASPELSYNC_URL: "http://hs:4000",
                CONF_HASPELSYNC_PRINTER_ID: SERIAL,
                CONF_SPOOLMAN_URL: "http://spoolman:7912",
            },
        )
        entry.runtime_data.state.history[:] = [PAST_JOB]  # 120 W average
        response = await _call(hass, "estimate_cost", {"file_path": str(path)})

    get_slots.assert_awaited_once_with(SERIAL)
    assert response["file"] == "lamp.gcode.3mf"
    assert response["estimated_duration_s"] == 12642
    assert [(f["spool_id"], f["slot"], f["spool_match"]) for f in response["filaments"]] == [
        (23, "A1", "ams"),
        (45, "A2", "ams"),
    ]
    filament = 62.4 * 0.020 + 22.2 * 0.025
    energy = 0.120 * 12642 / 3600
    assert response["average_power_w"] == pytest.approx(120.0)
    assert response["power_source"] == "history (1 prints)"
    assert response["filament_cost"] == pytest.approx(filament, abs=1e-4)
    assert response["energy_kwh"] == pytest.approx(energy, abs=1e-4)
    assert response["electricity_cost"] == pytest.approx(energy * 0.20, abs=1e-4)
    assert response["total_cost"] == pytest.approx(filament + energy * 0.20, abs=1e-3)


async def test_estimate_with_parameters_and_spoolman_down(hass: HomeAssistant, gcode_dir: Path) -> None:
    path = gcode_dir / "lamp.gcode"
    path.write_text(BAMBU_GCODE)
    with patch(
        "custom_components.ha_3d_print_cost.coordinator.SpoolmanClient.get_spool",
        AsyncMock(side_effect=SpoolmanError("timeout")),
    ):
        await _setup(hass, **{CONF_SPOOLMAN_URL: "http://spoolman:7912"})
        response = await _call(hass, "estimate_cost", {"file_path": str(path), "spool_ids": [23, 45], "power_w": 200})

    assert response["power_source"] == "parameter"
    assert response["energy_kwh"] == pytest.approx(0.2 * 4205 / 3600, abs=1e-4)
    assert response["filament_cost"] is None
    assert response["total_cost"] is None
    assert response["partial_total_cost"] == response["electricity_cost"]
    assert "Spoolman unavailable: timeout" in response["notes"]


async def test_estimate_rejects_disallowed_path_and_bad_file(hass: HomeAssistant, gcode_dir: Path) -> None:
    await _setup(hass)
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "estimate_cost", {"file_path": "/etc/passwd"})
    assert err.value.translation_key == "path_not_allowed"

    bad = gcode_dir / "empty.gcode"
    bad.write_text("G28\n")
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "estimate_cost", {"file_path": str(bad)})
    assert err.value.translation_key == "invalid_gcode"


# --------------------------------------------------------------------------- #
# Dashboard entities: history sensor, file select, estimate button
# --------------------------------------------------------------------------- #
async def test_history_sensor_attributes(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    job = {**PAST_JOB, "notes": ["n"], "filaments": [{"material": "PLA", "weight_g": 10, "cost": 0.2, "source": "x"}]}
    coordinator.state.history[:] = [job]
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    state = hass.states.get("sensor.x2d_print_history")
    assert state.state == "1"
    assert state.attributes["totals"]["total_cost"] == pytest.approx(0.32)
    jobs = state.attributes["jobs"]
    assert jobs[0]["name"] == "Benchy"
    assert "notes" not in jobs[0]
    assert jobs[0]["filaments"][0] == {
        "slot": None,
        "material": "PLA",
        "name": None,
        "vendor": None,
        "color": None,
        "weight_g": 10,
        "cost": 0.2,
    }


async def test_select_and_estimate_button(hass: HomeAssistant, gcode_dir: Path) -> None:
    (gcode_dir / "lamp.gcode").write_text(BAMBU_GCODE)
    with patch(
        "custom_components.ha_3d_print_cost.coordinator.SpoolmanClient.get_spool",
        side_effect=lambda spool_id: SPOOLS[spool_id],
    ):
        entry = await _setup(hass, **{CONF_SPOOLMAN_URL: "http://spoolman:7912", CONF_GCODE_FOLDER: str(gcode_dir)})
        select = hass.states.get("select.x2d_gcode_file")
        assert select.attributes["options"] == ["lamp.gcode"]
        assert select.state == "unknown"

        # Pressing without a selection is refused.
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call("button", "press", {"entity_id": "button.x2d_estimate_gcode"}, blocking=True)

        # A new file shows up after a refresh.
        make_3mf(gcode_dir / "flower.gcode.3mf", plates={1: BAMBU_GCODE})
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.x2d_refresh_gcode_files"}, blocking=True
        )
        assert "flower.gcode.3mf" in hass.states.get("select.x2d_gcode_file").attributes["options"]

        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": "select.x2d_gcode_file", "option": "flower.gcode.3mf"},
            blocking=True,
        )
        assert hass.states.get("select.x2d_gcode_file").state == "flower.gcode.3mf"
        entry.runtime_data.state.history[:] = [PAST_JOB]  # 120 W average
        await hass.services.async_call("button", "press", {"entity_id": "button.x2d_estimate_gcode"}, blocking=True)
        await hass.async_block_till_done()

    estimate = hass.states.get("sensor.x2d_gcode_estimate")
    assert estimate.attributes["file"] == "flower.gcode.3mf"
    assert estimate.attributes["estimated_duration_s"] == 12642
    assert estimate.attributes["power_source"] == "history (1 prints)"
    # No AMS / spool_ids from the dashboard: filament cost unknown, total too.
    assert estimate.state == "unknown"
    assert estimate.attributes["partial_total_cost"] > 0

    # The last estimate survives a reload.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.x2d_gcode_estimate").attributes["file"] == "flower.gcode.3mf"


async def test_estimate_service_updates_the_sensor(hass: HomeAssistant, gcode_dir: Path) -> None:
    path = gcode_dir / "lamp.gcode"
    path.write_text(BAMBU_GCODE)
    await _setup(hass)
    # Called without return_response (e.g. from an automation).
    await hass.services.async_call(DOMAIN, "estimate_cost", {"file_path": str(path), "power_w": 100}, blocking=True)
    await hass.async_block_till_done()
    state = hass.states.get("sensor.x2d_gcode_estimate")
    assert state.attributes["file"] == "lamp.gcode"
    assert state.attributes["energy_kwh"] == pytest.approx(0.1 * 4205 / 3600, abs=1e-4)
