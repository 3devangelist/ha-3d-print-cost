"""The "FDM Printers" sidebar panel and the live printer sensors it reads."""

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.components.frontend import DATA_PANELS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fdm_printer_manager.adapters.moonraker import KlipperStatus, MoonrakerConnectionError
from custom_components.fdm_printer_manager.const import DOMAIN, PANEL_URL_PATH

CLIENT = "custom_components.fdm_printer_manager.coordinator.MoonrakerClient"


async def test_first_add_integration_creates_the_panel_in_one_click(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "FDM Printers"
    assert result["description"] == "panel"
    await hass.async_block_till_done()
    assert PANEL_URL_PATH in hass.data[DATA_PANELS]
    panel = hass.data[DATA_PANELS][PANEL_URL_PATH]
    assert panel.sidebar_title == "FDM Printers"
    assert panel.require_admin

    # From then on: the printer types.
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["menu_options"] == ["klipper", "bambu_lan", "bambu_device"]

    # Gone when nothing of the integration is loaded any more.
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert not entry.supports_options
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert PANEL_URL_PATH not in hass.data[DATA_PANELS]


async def test_printer_entry_exposes_live_sensors_for_the_panel(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.escritorio_tomada_ender6", "on")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Ender 6",
        unique_id="klipper_192.168.1.89_7125",
        data={
            "printer_type": "klipper",
            "printer_name": "Ender 6",
            "host": "192.168.1.89",
            "port": 7125,
            "web_url": "http://192.168.1.89/",
            "switch_entity": "switch.escritorio_tomada_ender6",
            "integration_method": "left",
        },
    )
    entry.add_to_hass(hass)
    reading = KlipperStatus(
        state="printing", filename="cover.gcode", progress=41.2, print_duration_s=1800, nozzle_temp=214.8
    )
    with patch.multiple(
        CLIENT,
        status=AsyncMock(return_value=reading),
        metadata=AsyncMock(side_effect=MoonrakerConnectionError("no metadata")),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert PANEL_URL_PATH in hass.data[DATA_PANELS]
    status = hass.states.get("sensor.ender_6_printer_status")
    assert status.state == "printing"
    assert status.attributes["printer_type"] == "klipper"
    assert status.attributes["web_url"] == "http://192.168.1.89/"
    assert status.attributes["switch_entity"] == "switch.escritorio_tomada_ender6"
    assert status.attributes["entry_id"] == entry.entry_id
    assert "api_key" not in status.attributes
    assert float(hass.states.get("sensor.ender_6_print_progress").state) == 41.2
    assert hass.states.get("sensor.ender_6_print_file").state == "cover.gcode"
    assert float(hass.states.get("sensor.ender_6_nozzle_temperature").state) == 214.8
