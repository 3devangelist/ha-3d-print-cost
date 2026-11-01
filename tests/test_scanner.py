"""Klipper printers found by the network scan show up as discovery flows ("Found on the network")."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.fdm_printer_manager import scanner
from custom_components.fdm_printer_manager.adapters.netscan import FoundKlipper
from custom_components.fdm_printer_manager.const import DOMAIN

ADAPTERS = [
    {"name": "eth0", "enabled": True, "ipv4": [{"address": "192.168.1.10", "network_prefix": 24}], "ipv6": []},
    {"name": "docker0", "enabled": False, "ipv4": [{"address": "172.17.0.1", "network_prefix": 16}], "ipv6": []},
    {"name": "lo", "enabled": True, "ipv4": [{"address": "127.0.0.1", "network_prefix": 8}], "ipv6": []},
]


def _flows(hass: HomeAssistant) -> list[dict]:
    return [f for f in hass.config_entries.flow.async_progress() if f["handler"] == DOMAIN]


async def test_hosts_come_from_the_enabled_private_networks(hass: HomeAssistant) -> None:
    with patch.object(scanner.network, "async_get_adapters", AsyncMock(return_value=ADAPTERS)):
        hosts = await scanner.async_local_hosts(hass)
    assert len(hosts) == 253
    assert all(h.startswith("192.168.1.") for h in hosts)


async def test_found_printer_is_offered_once(hass: HomeAssistant) -> None:
    found = [FoundKlipper("192.168.1.95", 80)]
    with (
        patch.object(scanner, "async_local_hosts", AsyncMock(return_value=["192.168.1.95"])),
        patch.object(scanner, "async_scan", AsyncMock(return_value=found)),
        patch.object(scanner.MoonrakerClient, "hostname", AsyncMock(return_value="centauri")),
    ):
        assert await scanner.async_scan_network(hass) == found
        await scanner.async_scan_network(hass)  # 30 minutes later: still one card
    flows = _flows(hass)
    assert len(flows) == 1
    assert flows[0]["step_id"] == "zeroconf_confirm"
    assert flows[0]["context"]["title_placeholders"] == {"name": "centauri"}
    assert flows[0]["context"]["unique_id"] == "klipper_192.168.1.95_80"


async def test_configured_printer_is_not_offered(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="klipper_192.168.1.89_7125", data={}).add_to_hass(hass)
    with (
        patch.object(scanner, "async_local_hosts", AsyncMock(return_value=["192.168.1.89"])),
        patch.object(scanner, "async_scan", AsyncMock(return_value=[FoundKlipper("192.168.1.89", 7125)])),
        patch.object(scanner.MoonrakerClient, "hostname", AsyncMock(return_value="ender6")),
    ):
        await scanner.async_scan_network(hass)
    assert _flows(hass) == []


async def test_no_private_network_no_scan(hass: HomeAssistant) -> None:
    scan = AsyncMock()
    with (
        patch.object(scanner, "async_local_hosts", AsyncMock(return_value=[])),
        patch.object(scanner, "async_scan", scan),
    ):
        assert await scanner.async_scan_network(hass) == []
    scan.assert_not_called()


async def test_scans_at_start_and_every_30_minutes_until_stopped(hass: HomeAssistant) -> None:
    scan = AsyncMock(return_value=[])
    with patch.object(scanner, "async_scan_network", scan):
        stop = scanner.async_start_scanning(hass)
        await hass.async_block_till_done()
        assert scan.await_count == 1
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=31))
        await hass.async_block_till_done()
        assert scan.await_count == 2
        stop()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=62))
        await hass.async_block_till_done()
        assert scan.await_count == 2


async def test_a_failed_scan_is_logged_not_raised(hass: HomeAssistant, caplog) -> None:
    with patch.object(scanner, "async_scan_network", AsyncMock(side_effect=OSError("no network"))):
        stop = scanner.async_start_scanning(hass)
        await hass.async_block_till_done()
        stop()
    assert "Klipper network scan failed" in caplog.text


async def test_search_again_websocket(hass: HomeAssistant, hass_ws_client, panel_entry) -> None:
    assert await hass.config_entries.async_setup(panel_entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_ws_client(hass)
    with patch(
        "custom_components.fdm_printer_manager.async_scan_network",
        AsyncMock(return_value=[FoundKlipper("192.168.1.95", 80)]),
    ):
        await client.send_json({"id": 1, "type": f"{DOMAIN}/scan"})
        msg = await client.receive_json()
    assert msg["success"]
    assert msg["result"] == {"found": [{"host": "192.168.1.95", "port": 80}]}
