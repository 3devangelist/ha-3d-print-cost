"""Adding and editing a Klipper printer."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fdm_printer_manager.adapters.moonraker import MoonrakerAuthError, MoonrakerConnectionError
from custom_components.fdm_printer_manager.adapters.netscan import FoundKlipper
from custom_components.fdm_printer_manager.const import DOMAIN

CLIENT = "custom_components.fdm_printer_manager.config_flow.MoonrakerClient"
PROBE = "custom_components.fdm_printer_manager.config_flow.async_probe"

pytestmark = pytest.mark.usefixtures("panel_entry")


@pytest.fixture
async def bypass_setup(hass: HomeAssistant):
    with patch.multiple(
        "custom_components.fdm_printer_manager",
        async_setup_entry=AsyncMock(return_value=True),
        async_unload_entry=AsyncMock(return_value=True),
    ):
        yield
        for entry in hass.config_entries.async_entries(DOMAIN):
            await hass.config_entries.async_unload(entry.entry_id)


def _moonraker(hostname: str | None = "ender6", error: Exception | None = None):
    return patch.multiple(
        CLIENT,
        server_info=AsyncMock(side_effect=error, return_value={"klippy_state": "ready"}),
        hostname=AsyncMock(return_value=hostname),
    )


async def _open_klipper(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "klipper"})


async def test_add_klipper_printer_with_plug_only(hass: HomeAssistant, bypass_setup) -> None:
    hass.states.async_set("switch.escritorio_tomada_ender6", "on")
    result = await _open_klipper(hass)
    assert result["step_id"] == "klipper"
    with _moonraker():
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "192.168.1.89"})
    assert result["step_id"] == "electricity"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"switch_entity": "switch.escritorio_tomada_ender6"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "ender6"  # host name used when no name is typed
    data = result["data"]
    assert data["printer_type"] == "klipper"
    assert data["host"] == "192.168.1.89"
    assert data["port"] == 7125
    assert data["web_url"] == "http://192.168.1.89/"
    assert data["switch_entity"] == "switch.escritorio_tomada_ender6"
    assert "energy_entity" not in data


async def test_add_window_asks_only_the_ip(hass: HomeAssistant) -> None:
    result = await _open_klipper(hass)
    assert [str(k) for k in result["data_schema"].schema] == ["host"]


async def test_port_typed_with_the_ip(hass: HomeAssistant, bypass_setup) -> None:
    result = await _open_klipper(hass)
    with _moonraker():
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "192.168.1.89:7126"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["data"]["host"] == "192.168.1.89"
    assert result["data"]["port"] == 7126
    assert result["data"]["web_url"] == "http://192.168.1.89/"


async def test_cannot_connect(hass: HomeAssistant) -> None:
    result = await _open_klipper(hass)
    with _moonraker(error=MoonrakerConnectionError("timeout")):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "192.168.1.250"})
    assert result["errors"] == {"host": "cannot_connect"}


async def test_api_key_asked_only_when_moonraker_wants_one(hass: HomeAssistant, bypass_setup) -> None:
    result = await _open_klipper(hass)
    with _moonraker(error=MoonrakerAuthError("401")):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "192.168.1.89"})
    assert result["step_id"] == "klipper_auth"
    with _moonraker(error=MoonrakerAuthError("401")):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_key": "wrong"})
    assert result["errors"] == {"api_key": "invalid_auth"}
    with _moonraker():
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_key": "right"})
    assert result["step_id"] == "electricity"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["data"]["api_key"] == "right"


async def test_same_klipper_twice_aborts(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="klipper_192.168.1.89_7125", data={}).add_to_hass(hass)
    result = await _open_klipper(hass)
    with _moonraker():
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "192.168.1.89"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_edit_klipper_ip_keeps_the_entry(hass: HomeAssistant, bypass_setup) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Ender 6",
        unique_id="klipper_192.168.1.89_7125",
        data={
            "printer_type": "klipper",
            "printer_name": "Ender 6",
            "host": "192.168.1.89",
            "port": 7125,
            "api_key": "old",
            "web_url": "http://192.168.1.89/",
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "klipper"
    with _moonraker():
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"printer_name": "Ender 6", "host": "192.168.1.91", "port": 7125}
        )
    assert result["step_id"] == "electricity"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "behaviour"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"update_interval": 30, "currency": "EUR"}
    )
    assert result["step_id"] == "alerts"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "finish"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    merged = {**entry.data, **entry.options}
    assert merged["host"] == "192.168.1.91"
    assert merged["web_url"] == "http://192.168.1.91/"
    assert merged["api_key"] == "old"  # left empty: the stored key is kept (it is never sent to the form)


async def test_ip_only_finds_moonraker_on_port_80(hass: HomeAssistant, bypass_setup) -> None:
    """Some printers (Elegoo Centauri Carbon) serve Moonraker on port 80 only."""
    result = await _open_klipper(hass)
    probe = AsyncMock(return_value=FoundKlipper("192.168.1.95", 80))
    with patch(PROBE, probe), _moonraker(hostname="centauri"):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "192.168.1.95"})
    assert probe.await_args.args[1:] == ("192.168.1.95", (7125, 80))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["data"]["port"] == 80
    assert result["data"]["web_url"] == "http://192.168.1.95/"


async def test_typed_port_is_not_probed(hass: HomeAssistant, bypass_setup) -> None:
    result = await _open_klipper(hass)
    probe = AsyncMock()
    with patch(PROBE, probe), _moonraker():
        await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "192.168.1.89:7126"})
    probe.assert_not_called()


async def test_found_by_the_network_scan(hass: HomeAssistant, bypass_setup) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data={"printer_type": "klipper", "host": "192.168.1.95", "port": 80, "printer_name": "centauri"},
    )
    assert result["step_id"] == "zeroconf_confirm"
    assert result["description_placeholders"] == {"name": "centauri", "host": "192.168.1.95"}
    with _moonraker(hostname="centauri"):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "electricity"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["host"] == "192.168.1.95"
    assert result["data"]["port"] == 80
    assert result["result"].unique_id == "klipper_192.168.1.95_80"


async def test_found_by_the_scan_but_already_added(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="klipper_192.168.1.95_80", data={}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data={"printer_type": "klipper", "host": "192.168.1.95", "port": 80, "printer_name": None},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_auto_off_and_ssh_options(hass: HomeAssistant, bypass_setup) -> None:
    """0.8.0: auto-off (on/off since 0.12.3); SSH login for Klipper, an empty password keeps the stored one."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Ender 6",
        unique_id="klipper_192.168.1.89_7125",
        data={"printer_type": "klipper", "printer_name": "Ender 6", "host": "192.168.1.89", "port": 7125},
        options={"ssh_username": "pi", "ssh_password": "stored"},
    )
    entry.add_to_hass(hass)

    async def behaviour(values: dict) -> dict:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        with _moonraker():
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"printer_name": "Ender 6", "host": "192.168.1.89", "port": 7125}
            )
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
        assert result["step_id"] == "behaviour"
        fields = {str(k) for k in result["data_schema"].schema}
        assert {"auto_off", "ssh_username", "ssh_password", "ssh_private_key", "ssh_command"} <= fields
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"update_interval": 30, "currency": "EUR", **values}
        )
        assert result["step_id"] == "alerts"
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
        assert result["step_id"] == "finish"
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        return {**entry.data, **entry.options}

    merged = await behaviour({"auto_off": True, "ssh_username": "pi", "ssh_command": "sudo poweroff"})
    assert merged["auto_off"] is True
    assert merged["ssh_password"] == "stored"  # left empty: kept
    assert merged["ssh_command"] == "sudo poweroff"

    merged = await behaviour({"auto_off": False})
    assert merged["auto_off"] is False
    assert merged["ssh_username"] is None  # no user name: the login is forgotten
    assert merged["ssh_password"] is None
