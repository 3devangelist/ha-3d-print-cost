"""Config and options flows."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fdm_printer_manager.const import (
    CONF_ENERGY_ENTITY,
    CONF_INTEGRATION_METHOD,
    CONF_JOB_NAME_ENTITY,
    CONF_POWER_ENTITY,
    CONF_PRICE_ENTITY,
    CONF_PRINT_STATUS_ENTITY,
    CONF_PRINTER_DEVICE_ID,
    CONF_PRINTER_SERIAL,
    DOMAIN,
)

from .helpers import BAMBU_SENSORS, SERIAL, register_bambu_printer


@pytest.fixture
async def bypass_setup(hass: HomeAssistant):
    """Create entries without setting them up (setup and unload patched)."""
    with patch.multiple(
        "custom_components.fdm_printer_manager",
        async_setup_entry=AsyncMock(return_value=True),
        async_unload_entry=AsyncMock(return_value=True),
    ):
        yield
        for entry in hass.config_entries.async_entries(DOMAIN):
            await hass.config_entries.async_unload(entry.entry_id)


ELEC = {
    CONF_POWER_ENTITY: "sensor.x2d_power",
    CONF_ENERGY_ENTITY: "sensor.x2d_energy",
    CONF_PRICE_ENTITY: "sensor.prix_electricite_octopus",
    CONF_INTEGRATION_METHOD: "left",
}


def set_electric_states(hass: HomeAssistant, price_unit: str = "EUR/kWh") -> None:
    hass.states.async_set("sensor.x2d_power", "12", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.x2d_energy", "100.0", {"unit_of_measurement": "kWh"})
    hass.states.async_set("sensor.prix_electricite_octopus", "0.1876", {"unit_of_measurement": price_unit})


async def _start_bambu(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"][:2] == ["klipper", "bambu_device"]
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "bambu_device"})


async def _go_to_electricity(hass: HomeAssistant):
    device_id = register_bambu_printer(hass)
    result = await _start_bambu(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PRINTER_DEVICE_ID: device_id})
    assert result["step_id"] == "bambu"
    assert result["description_placeholders"]["found"] == "4"
    # suggested values come from discovery by translation_key, not entity_id
    schema = {str(k): k for k in result["data_schema"].schema}
    assert schema[CONF_PRINT_STATUS_ENTITY].description["suggested_value"] == BAMBU_SENSORS["print_status"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_PRINT_STATUS_ENTITY: BAMBU_SENSORS["print_status"], CONF_JOB_NAME_ENTITY: BAMBU_SENSORS["subtask_name"]},
    )
    assert result["step_id"] == "electricity"
    return device_id, result


async def test_full_flow_electricity_only(hass: HomeAssistant, bypass_setup) -> None:
    set_electric_states(hass)
    device_id, result = await _go_to_electricity(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ELEC)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "X2D"
    assert result["data"]["printer_type"] == "bambu"
    data = result["data"]
    assert data[CONF_PRINTER_DEVICE_ID] == device_id
    assert data[CONF_PRINTER_SERIAL] == SERIAL
    assert data[CONF_PRICE_ENTITY] == "sensor.prix_electricite_octopus"
    assert not any("filament" in key or "haspelsync" in key for key in data)


async def test_price_required_with_a_meter(hass: HomeAssistant) -> None:
    set_electric_states(hass)
    _, result = await _go_to_electricity(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ENERGY_ENTITY: "sensor.x2d_energy", CONF_INTEGRATION_METHOD: "left"}
    )
    assert result["errors"] == {CONF_PRICE_ENTITY: "price_required"}


async def test_plug_without_meter_is_accepted(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.x2d_plug", "on")
    _, result = await _go_to_electricity(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"switch_entity": "switch.x2d_plug", CONF_INTEGRATION_METHOD: "left"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_unsupported_price_unit(hass: HomeAssistant) -> None:
    set_electric_states(hass, price_unit="EUR/m3")
    _, result = await _go_to_electricity(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ELEC)
    assert result["errors"] == {CONF_PRICE_ENTITY: "unsupported_price_unit"}


async def test_same_printer_twice_aborts(hass: HomeAssistant) -> None:
    device_id = register_bambu_printer(hass)
    MockConfigEntry(domain=DOMAIN, unique_id=device_id, data={}).add_to_hass(hass)
    result = await _start_bambu(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PRINTER_DEVICE_ID: device_id})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow_changes_price_and_clears_energy(hass: HomeAssistant, bypass_setup) -> None:
    set_electric_states(hass)
    hass.states.async_set("sensor.tempo_price", "15.2", {"unit_of_measurement": "c€/kWh"})
    device_id = register_bambu_printer(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=device_id,
        data={
            CONF_PRINTER_DEVICE_ID: device_id,
            CONF_PRINTER_SERIAL: SERIAL,
            CONF_PRINT_STATUS_ENTITY: BAMBU_SENSORS["print_status"],
            **ELEC,
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PRINT_STATUS_ENTITY: BAMBU_SENSORS["print_status"]}
    )
    assert result["step_id"] == "electricity"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_POWER_ENTITY: "sensor.x2d_power",
            CONF_PRICE_ENTITY: "sensor.tempo_price",
            CONF_INTEGRATION_METHOD: "trapezoidal",
        },
    )
    assert result["step_id"] == "behaviour"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"update_interval": 20, "currency": "eur"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    merged = {**entry.data, **entry.options}
    assert merged[CONF_PRICE_ENTITY] == "sensor.tempo_price"
    assert merged[CONF_ENERGY_ENTITY] is None  # cleared field really cleared
    assert merged[CONF_INTEGRATION_METHOD] == "trapezoidal"
    assert merged["currency"] == "EUR"
