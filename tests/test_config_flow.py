"""Config and options flows."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_3d_print_cost.adapters.haspelsync import HaspelsyncAuthError, HaspelsyncPrinter
from custom_components.ha_3d_print_cost.const import (
    CONF_ENERGY_ENTITY,
    CONF_FILAMENT_FALLBACK,
    CONF_HASPELSYNC_API_KEY,
    CONF_HASPELSYNC_PRINTER_ID,
    CONF_HASPELSYNC_URL,
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
        "custom_components.ha_3d_print_cost",
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


async def _go_to_electricity(hass: HomeAssistant):
    device_id = register_bambu_printer(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PRINTER_DEVICE_ID: device_id})
    assert result["step_id"] == "bambu"
    assert result["description_placeholders"]["found"] == "5"
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
    assert result["step_id"] == "filament"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_FILAMENT_FALLBACK: "planned_success"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "3D Print Cost — X2D"
    data = result["data"]
    assert data[CONF_PRINTER_DEVICE_ID] == device_id
    assert data[CONF_PRINTER_SERIAL] == SERIAL
    assert data[CONF_PRICE_ENTITY] == "sensor.prix_electricite_octopus"
    assert CONF_HASPELSYNC_URL not in data


async def test_power_or_energy_required(hass: HomeAssistant) -> None:
    set_electric_states(hass)
    _, result = await _go_to_electricity(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PRICE_ENTITY: "sensor.prix_electricite_octopus", CONF_INTEGRATION_METHOD: "left"}
    )
    assert result["errors"] == {"base": "power_or_energy_required"}


async def test_unsupported_price_unit(hass: HomeAssistant) -> None:
    set_electric_states(hass, price_unit="EUR/m3")
    _, result = await _go_to_electricity(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ELEC)
    assert result["errors"] == {CONF_PRICE_ENTITY: "unsupported_price_unit"}


async def test_haspelsync_printer_matched_by_serial(hass: HomeAssistant, bypass_setup) -> None:
    set_electric_states(hass)
    _, result = await _go_to_electricity(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ELEC)
    printers = [HaspelsyncPrinter("OTHER", "P2S"), HaspelsyncPrinter(SERIAL, "X2D")]
    with (
        patch(
            "custom_components.ha_3d_print_cost.config_flow.HaspelsyncClient.get_printers",
            AsyncMock(return_value=printers),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HASPELSYNC_URL: "http://192.168.1.10:4000",
                CONF_HASPELSYNC_API_KEY: "secret",
                CONF_FILAMENT_FALLBACK: "none",
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HASPELSYNC_PRINTER_ID] == SERIAL


async def test_haspelsync_bad_key(hass: HomeAssistant) -> None:
    set_electric_states(hass)
    _, result = await _go_to_electricity(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ELEC)
    with patch(
        "custom_components.ha_3d_print_cost.config_flow.HaspelsyncClient.get_printers",
        AsyncMock(side_effect=HaspelsyncAuthError("401")),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HASPELSYNC_URL: "http://hs:4000", CONF_HASPELSYNC_API_KEY: "bad", CONF_FILAMENT_FALLBACK: "none"},
        )
    assert result["errors"] == {CONF_HASPELSYNC_API_KEY: "invalid_auth"}


async def test_same_printer_twice_aborts(hass: HomeAssistant) -> None:
    device_id = register_bambu_printer(hass)
    MockConfigEntry(domain=DOMAIN, unique_id=device_id, data={}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
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
            CONF_FILAMENT_FALLBACK: "none",
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
    assert result["step_id"] == "filament"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_FILAMENT_FALLBACK: "none"})
    assert result["step_id"] == "behaviour"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"update_interval": 20, "history_size": 50, "currency": "eur"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    merged = {**entry.data, **entry.options}
    assert merged[CONF_PRICE_ENTITY] == "sensor.tempo_price"
    assert merged[CONF_ENERGY_ENTITY] is None  # cleared field really cleared
    assert merged[CONF_INTEGRATION_METHOD] == "trapezoidal"
    assert merged["currency"] == "EUR"
