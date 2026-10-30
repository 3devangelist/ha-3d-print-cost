"""Bambu Lab over the local network: MQTT packets, reports, SSDP, a fake broker, flows and discovery."""

import asyncio
import json
import struct
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fdm_printer_manager.adapters.bambu_lan import (
    BambuAuthError,
    BambuConnectionError,
    BambuLanClient,
    BambuStatus,
    command,
    packet_connect,
    packet_publish,
    packet_subscribe,
    parse_publish,
    parse_report,
    parse_ssdp,
)
from custom_components.fdm_printer_manager.const import DOMAIN

SERIAL = "03919A3B2001934"
TEST = "custom_components.fdm_printer_manager.config_flow.BambuLanClient.async_test"
CLIENT = "custom_components.fdm_printer_manager.coordinator.MoonrakerClient"

# --------------------------------------------------------------------------- #
# Pure functions
# --------------------------------------------------------------------------- #


def test_connect_packet() -> None:
    pkt = packet_connect("fdm-001934", "bblp", "12345678")
    assert pkt[0] == 0x10
    assert pkt[2:8] == b"\x00\x04MQTT"
    assert pkt[8] == 4  # MQTT 3.1.1
    assert pkt[9] == 0xC2  # username + password + clean session
    assert pkt.endswith(b"\x00\x04bblp\x00\x0812345678")


def test_publish_round_trip_and_subscribe() -> None:
    payload = json.dumps({"print": {"gcode_state": "RUNNING"}}).encode()
    pkt = packet_publish(f"device/{SERIAL}/report", payload)
    assert pkt[0] == 0x30
    body = pkt[2:]  # one-byte remaining length for short packets
    assert parse_publish(pkt[0], body) == (f"device/{SERIAL}/report", payload)
    sub = packet_subscribe(1, f"device/{SERIAL}/report")
    assert sub[0] == 0x82
    assert sub.endswith(b"\x00")


def test_long_packet_remaining_length() -> None:
    pkt = packet_publish("t", b"x" * 300)
    assert pkt[1] & 0x80  # two-byte remaining length


def test_report_parsing_and_commands() -> None:
    merged = {
        "gcode_state": "RUNNING",
        "mc_percent": 41,
        "mc_remaining_time": 42,
        "subtask_name": "Statue",
        "nozzle_temper": 219.8,
    }
    assert parse_report(merged) == BambuStatus("running", "Statue", 41.0, 42.0, 219.8)
    assert parse_report({}).state == "idle"
    assert json.loads(command("stop")) == {"print": {"sequence_id": "0", "command": "stop", "param": ""}}


def test_ssdp_parsing() -> None:
    msg = (
        b"NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:1990\r\nServer: UPnP/1.0\r\nLocation: 192.168.1.90\r\n"
        b"NT: urn:bambulab-com:device:3dprinter:1\r\nUSN: 03919A3B2001934\r\nDevModel.bambu.com: N2S\r\n"
        b"DevName.bambu.com: A1\r\nDevConnect.bambu.com: lan\r\n\r\n"
    )
    found = parse_ssdp(msg, "192.168.1.90")
    assert found.serial == SERIAL
    assert found.host == "192.168.1.90"
    assert found.name == "A1"
    assert found.model == "N2S"
    assert parse_ssdp(b"NOTIFY * HTTP/1.1\r\nNT: upnp:rootdevice\r\nUSN: x\r\n\r\n", "1.2.3.4") is None


# --------------------------------------------------------------------------- #
# Fake MQTT broker (plain TCP)
# --------------------------------------------------------------------------- #
async def _read(reader):
    first = (await reader.readexactly(1))[0]
    mult = length = 0
    mult = 1
    while True:
        b = (await reader.readexactly(1))[0]
        length += (b & 0x7F) * mult
        if not b & 0x80:
            break
        mult *= 128
    return first, await reader.readexactly(length)


async def _broker(code: str, reports: list[dict], received: list):
    async def handle(reader, writer):
        first, body = await _read(reader)
        pw_len = struct.unpack("!H", body[-len(code) - 2 : -len(code)])[0] if len(body) > len(code) + 2 else 0
        ok = body.endswith(code.encode()) and pw_len == len(code)
        writer.write(bytes([0x20, 0x02, 0x00, 0x00 if ok else 0x05]))
        await writer.drain()
        if not ok:
            writer.close()
            return
        try:
            while True:
                first, body = await asyncio.wait_for(_read(reader), 2)
                received.append((first >> 4, body))
                if first >> 4 == 3:  # a request: answer with the reports
                    _topic, payload = parse_publish(first, body)
                    if b"pushall" in payload:
                        for rep in reports:
                            writer.write(packet_publish(f"device/{SERIAL}/report", json.dumps({"print": rep}).encode()))
                        await writer.drain()
        except (TimeoutError, asyncio.IncompleteReadError):
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


@pytest.mark.usefixtures("socket_enabled")
async def test_client_against_fake_broker() -> None:
    received: list = []
    server, port = await _broker(
        "12345678", [{"gcode_state": "RUNNING", "mc_percent": 10}, {"mc_percent": 12}], received
    )
    async with server:
        client = BambuLanClient("127.0.0.1", SERIAL, "12345678", lambda: None, port=port, use_tls=False)
        status = await client.async_test(timeout=3)
        assert status.state == "running"

        updates: list[int] = []
        live = BambuLanClient("127.0.0.1", SERIAL, "12345678", lambda: updates.append(1), port=port, use_tls=False)
        live.start(lambda coro: asyncio.get_running_loop().create_task(coro))
        for _ in range(50):
            if live.available and live.reading.progress == 12:
                break
            await asyncio.sleep(0.05)
        assert live.reading.state == "running"
        assert live.reading.progress == 12
        await live.async_command("pause")
        await asyncio.sleep(0.1)
        await live.stop()
        sent = [json.loads(parse_publish(0x30, b)[1]) for t, b in received if t == 3]
        assert {"print": {"sequence_id": "0", "command": "pause", "param": ""}} in sent

        bad = BambuLanClient("127.0.0.1", SERIAL, "wrong", lambda: None, port=port, use_tls=False)
        with pytest.raises(BambuAuthError):
            await bad.async_test(timeout=3)


@pytest.mark.usefixtures("socket_enabled")
async def test_client_unreachable() -> None:
    client = BambuLanClient("127.0.0.1", SERIAL, "x", lambda: None, port=1, use_tls=False)
    with pytest.raises(BambuConnectionError):
        await client.async_test(timeout=1)


# --------------------------------------------------------------------------- #
# Flows
# --------------------------------------------------------------------------- #
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


@pytest.mark.usefixtures("panel_entry", "bypass_setup")
async def test_add_bambu_lan(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "bambu_lan"})
    assert [str(k) for k in result["data_schema"].schema] == ["printer_name", "host", "printer_serial", "access_code"]
    with patch(TEST, AsyncMock(side_effect=BambuAuthError("no"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.90", "printer_serial": SERIAL.lower(), "access_code": "bad"}
        )
    assert result["errors"] == {"access_code": "invalid_auth"}
    with patch(TEST, AsyncMock(side_effect=BambuConnectionError("no"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.91", "printer_serial": SERIAL, "access_code": "x"}
        )
    assert result["errors"] == {"base": "cannot_connect"}
    with patch(TEST, AsyncMock(return_value=BambuStatus("idle"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.90", "printer_serial": SERIAL, "access_code": "12345678"}
        )
    assert result["step_id"] == "electricity"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Bambu 1934"
    assert result["data"]["printer_type"] == "bambu_lan"
    assert result["data"]["printer_serial"] == SERIAL
    assert result["result"].unique_id == f"bambu_{SERIAL}"


@pytest.mark.usefixtures("panel_entry", "bypass_setup")
async def test_bambu_discovery_asks_only_the_access_code(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data={"host": "192.168.1.90", "printer_serial": SERIAL, "printer_name": "A1"},
    )
    assert result["step_id"] == "bambu_lan"
    schema = {str(k): k for k in result["data_schema"].schema}
    assert schema["host"].description["suggested_value"] == "192.168.1.90"
    assert schema["printer_serial"].description["suggested_value"] == SERIAL
    with patch(TEST, AsyncMock(return_value=BambuStatus("idle"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.90", "printer_serial": SERIAL, "access_code": "12345678"}
        )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["title"] == "A1"

    # Heard again with a new IP: no new flow, the entry's IP is updated.
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
        data={"host": "192.168.1.99", "printer_serial": SERIAL, "printer_name": "A1"},
    )
    assert result["type"] is FlowResultType.ABORT
    entry = next(e for e in hass.config_entries.async_entries(DOMAIN) if e.unique_id == f"bambu_{SERIAL}")
    assert entry.data["host"] == "192.168.1.99"


@pytest.mark.usefixtures("panel_entry", "bypass_setup")
async def test_klipper_zeroconf_discovery(hass: HomeAssistant) -> None:
    info = ZeroconfServiceInfo(
        ip_address=__import__("ipaddress").ip_address("192.168.1.89"),
        ip_addresses=[__import__("ipaddress").ip_address("192.168.1.89")],
        hostname="ender6.local.",
        name="Moonraker Instance on ender6._moonraker._tcp.local.",
        port=7125,
        type="_moonraker._tcp.local.",
        properties={},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info
    )
    assert result["step_id"] == "zeroconf_confirm"
    assert result["description_placeholders"] == {"name": "ender6", "host": "192.168.1.89"}
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows[0]["context"]["title_placeholders"] == {"name": "ender6"}
    with patch.multiple(
        "custom_components.fdm_printer_manager.config_flow.MoonrakerClient",
        server_info=AsyncMock(return_value={"klippy_state": "ready"}),
        hostname=AsyncMock(return_value="ender6"),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "electricity"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "ender6"
    assert result["data"]["host"] == "192.168.1.89"

    # Same printer announced again: nothing new.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info
    )
    assert result["type"] is FlowResultType.ABORT


async def test_ssdp_announcement_starts_one_discovery_flow(hass: HomeAssistant) -> None:
    from custom_components.fdm_printer_manager import _on_bambu_found  # noqa: PLC0415
    from custom_components.fdm_printer_manager.adapters.bambu_lan import BambuAnnouncement  # noqa: PLC0415

    MockConfigEntry(domain=DOMAIN, unique_id="panel", data={"panel": True}).add_to_hass(hass)
    found = BambuAnnouncement(host="192.168.1.90", serial=SERIAL, name="A1", model="N2S")
    _on_bambu_found(hass, found)
    _on_bambu_found(hass, found)  # announced every few seconds: only one flow
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["step_id"] == "bambu_lan"
    assert flows[0]["context"]["title_placeholders"] == {"name": "A1"}
