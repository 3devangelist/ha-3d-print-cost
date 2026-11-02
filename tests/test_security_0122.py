"""0.12.2 security fixes: fake printers, size limits, admin-only actions, secrets, local OCR, baby steps."""

import asyncio
import hashlib
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service
from pytest_homeassistant_custom_component.test_util.aiohttp import mock_stream

from custom_components.fdm_printer_manager.adapters.bambu_lan import (
    MAX_PACKET,
    BambuConnectionError,
    BambuIdentityError,
    BambuLanClient,
    read_packet,
)
from custom_components.fdm_printer_manager.adapters.klipper_extras import babystep_gcode
from custom_components.fdm_printer_manager.adapters.moonraker import parse_webcams, read_limited
from custom_components.fdm_printer_manager.adapters.ssh import COSMOS_LOGIN, SshHostKeyChanged, ssh_shutdown
from custom_components.fdm_printer_manager.config_flow import bambu_lan_schema, klipper_schema
from custom_components.fdm_printer_manager.const import DOMAIN, PrinterType
from custom_components.fdm_printer_manager.diagnostics import async_get_config_entry_diagnostics
from custom_components.fdm_printer_manager.identity import (
    KIND_MACHINE,
    KIND_SSH,
    KIND_TLS,
    PrinterIdentity,
    klipper_machine_id,
)

from .helpers import BAMBU_SENSORS, register_bambu_printer
from .test_auto_off import (
    Machine,
    attrs,
    machine,  # noqa: F401 - a fixture
    setup,
)
from .test_controls import tick
from .test_power_adapters import connection, fake_asyncssh

COMPONENT = Path(__file__).parent.parent / "custom_components" / "fdm_printer_manager"
SAFE = "switch.ender_6_safe_power"


# --------------------------------------------------------------------------- #
# identity.py
# --------------------------------------------------------------------------- #
def test_identity_learns_then_compares() -> None:
    identity = PrinterIdentity()
    assert identity.check(KIND_TLS, "aa") == (True, True)  # first contact: remembered
    assert identity.check(KIND_TLS, "aa") == (True, False)
    assert identity.check(KIND_TLS, None) == (True, False)  # nothing seen: no verdict
    assert identity.check(KIND_TLS, "bb") == (False, False)
    assert identity.changed
    assert identity.describe() == "its TLS certificate"
    assert identity.check(KIND_TLS, None) == (False, False)  # still blocked
    assert identity.check(KIND_TLS, "aa") == (True, False)  # the real printer is back
    assert not identity.changed


def test_identity_accept_keeps_the_new_value() -> None:
    identity = PrinterIdentity({KIND_TLS: "aa", KIND_SSH: "ssh-ed25519 OLD"})
    identity.check(KIND_TLS, "bb")
    identity.refuse(KIND_SSH)  # SSH refused the key: the new one is not known
    identity.accept()
    assert not identity.changed
    assert identity.known == {KIND_TLS: "bb"}  # the SSH key is learnt again at next use


def test_klipper_machine_id() -> None:
    pi = {"cpu_info": {"serial_number": "10000000ABCDEF01"}, "network": {"eth0": {"mac_address": "aa:bb"}}}
    assert klipper_machine_id(pi, "ender6") == "cpu:10000000abcdef01"
    no_serial = {
        "cpu_info": {"serial_number": "0000"},
        "network": {
            "wlan0": {"mac_address": "AA:01"},
            "eth0": {"mac_address": "aa:02"},
            "lo": {"mac_address": "00:00:00:00:00:00"},
        },
    }
    assert klipper_machine_id(no_serial, "x") == "mac:aa:01,aa:02"
    assert klipper_machine_id(None, "Cosmos") == "host:cosmos"
    assert klipper_machine_id({}, None) is None


def test_mac_identity_survives_a_changed_network_card() -> None:
    identity = PrinterIdentity({KIND_MACHINE: "mac:aa:01,aa:02"})
    assert identity.check(KIND_MACHINE, "mac:aa:02,aa:03")[0]  # one card in common
    assert not identity.check(KIND_MACHINE, "mac:bb:01")[0]
    other = PrinterIdentity({KIND_MACHINE: "cpu:1234"})
    assert other.check(KIND_MACHINE, "host:cosmos")[0]  # cannot compare: no verdict


# --------------------------------------------------------------------------- #
# Klipper: another computer at the printer's address
# --------------------------------------------------------------------------- #
async def test_klipper_with_another_machine_id_is_blocked_until_accepted(
    hass: HomeAssistant,
    freezer,
    machine: Machine,  # noqa: F811
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    ident = AsyncMock(return_value="cpu:1111")
    with patch("custom_components.fdm_printer_manager.adapters.moonraker.MoonrakerClient.machine_identity", ident):
        entry = await setup(hass)
        coordinator = entry.runtime_data
        assert coordinator.identity.known == {KIND_MACHINE: "cpu:1111"}
        cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")

        ident.return_value = "cpu:2222"  # something else answers at 192.168.1.89
        await tick(hass, freezer, 5)
        with pytest.raises(HomeAssistantError, match="does not look like the same printer"):
            await hass.services.async_call("switch", "turn_off", {"entity_id": SAFE}, blocking=True)
        assert cut == []
        assert machine.calls == []
        assert attrs(hass)["identity_ok"] is False
        assert (DOMAIN, f"identity_changed_{entry.entry_id}") in ir.async_get(hass).issues
        assert not coordinator.can_control("pause")

        # The admin checks the printer and accepts it in the options flow.
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["step_id"] == "identity"
        assert result["description_placeholders"]["what"] == "its computer (Moonraker machine identity)"
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"accept_identity": True})
        assert result["step_id"] == "klipper"
        assert coordinator.identity.known == {KIND_MACHINE: "cpu:2222"}
        assert not coordinator.identity.changed
        assert (DOMAIN, f"identity_changed_{entry.entry_id}") not in ir.async_get(hass).issues


async def test_identity_is_stored(hass: HomeAssistant, machine: Machine, hass_storage) -> None:  # noqa: F811
    with patch(
        "custom_components.fdm_printer_manager.adapters.moonraker.MoonrakerClient.machine_identity",
        AsyncMock(return_value="cpu:1111"),
    ):
        entry = await setup(hass)
        await hass.async_block_till_done()
    assert hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["identity"] == {KIND_MACHINE: "cpu:1111"}


# --------------------------------------------------------------------------- #
# Bambu: offline through ha-bambulab (finding 3), and the MQTT broker's identity
# --------------------------------------------------------------------------- #
async def test_bambu_offline_is_never_cut(hass: HomeAssistant) -> None:
    device_id = register_bambu_printer(hass)
    plug = "switch.x2d_plug"
    hass.states.async_set(plug, "on")
    hass.states.async_set(BAMBU_SENSORS["print_status"], "offline")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="X2D",
        unique_id=device_id,
        data={
            "printer_device_id": device_id,
            "printer_name": "X2D",
            "print_status_entity": BAMBU_SENSORS["print_status"],
            "switch_entity": plug,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")
    with pytest.raises(HomeAssistantError, match="no contact with the printer"):
        await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.x2d_safe_power"}, blocking=True)
    assert cut == []

    hass.states.async_set(BAMBU_SENSORS["print_status"], "idle")  # answers again: cut as before
    await hass.async_block_till_done()
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.x2d_safe_power"}, blocking=True)
    assert len(cut) == 1


async def _reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def test_mqtt_packet_size_is_capped() -> None:
    with pytest.raises(BambuConnectionError, match="too large"):
        await read_packet(await _reader(bytes([0x30, 0xFF, 0xFF, 0xFF, 0x7F])))  # 256 MB announced
    with pytest.raises(BambuConnectionError, match="malformed"):
        await read_packet(await _reader(bytes([0x30, 0xFF, 0xFF, 0xFF, 0xFF, 0x01])))  # 5 length bytes
    big = MAX_PACKET + 1
    length = bytes([(big & 0x7F) | 0x80, ((big >> 7) & 0x7F) | 0x80, big >> 14])
    with pytest.raises(BambuConnectionError):
        await read_packet(await _reader(bytes([0x30]) + length))
    assert await read_packet(await _reader(bytes([0x30, 0x02, 0x01, 0x02]))) == (0x30, b"\x01\x02")


@pytest.mark.usefixtures("socket_enabled")
async def test_bambu_identity_refused_before_the_access_code_is_sent() -> None:
    received: list[bytes] = []

    async def handle(reader, writer):
        received.append(await reader.read(100))
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        client = BambuLanClient(
            "127.0.0.1", "SERIAL", "12345678", lambda: None, port=port, use_tls=False, check_identity=lambda fp: False
        )
        with pytest.raises(BambuIdentityError):
            await client._open()
        await asyncio.sleep(0.1)
    assert b"12345678" not in b"".join(received)


# --------------------------------------------------------------------------- #
# SSH host key (finding 2)
# --------------------------------------------------------------------------- #
async def test_ssh_first_use_returns_the_host_key() -> None:
    conn = connection()
    key = MagicMock()
    key.export_public_key.return_value = b"ssh-ed25519 AAAAfirst\n"
    conn.get_server_host_key = MagicMock(return_value=key)
    mod = fake_asyncssh(conn)
    with patch.dict(sys.modules, {"asyncssh": mod}):
        assert await ssh_shutdown("192.168.1.95", COSMOS_LOGIN) == "ssh-ed25519 AAAAfirst"
    assert mod.connect.await_args.kwargs["known_hosts"] is None


async def test_ssh_only_accepts_the_remembered_key() -> None:
    conn = connection()
    mod = fake_asyncssh(conn)
    mod.import_public_key = MagicMock(return_value="PUBKEY")

    class HostKeyNotVerifiable(mod.DisconnectError):
        pass

    mod.HostKeyNotVerifiable = HostKeyNotVerifiable
    with patch.dict(sys.modules, {"asyncssh": mod}):
        await ssh_shutdown("192.168.1.95", COSMOS_LOGIN, host_key="ssh-ed25519 AAAAfirst")
        assert mod.connect.await_args.kwargs["known_hosts"] == (["PUBKEY"], [], [])
        mod.connect = AsyncMock(side_effect=HostKeyNotVerifiable("changed"))
        with pytest.raises(SshHostKeyChanged):
            await ssh_shutdown("192.168.1.95", COSMOS_LOGIN, host_key="ssh-ed25519 AAAAfirst")


def test_ssh_docstring_no_longer_says_no_shell() -> None:
    """Finding 16: the command runs in the remote login shell; the comment says so."""
    text = (COMPONENT / "adapters" / "ssh.py").read_text("utf-8")
    assert "no shell features" not in text
    assert "known_hosts=None``): the printer is on the local" not in text


# --------------------------------------------------------------------------- #
# Moonraker answers and camera URLs (findings 8, 11)
# --------------------------------------------------------------------------- #
async def test_read_limited() -> None:
    small = MagicMock(content=mock_stream(b"x" * 10), content_length=10)
    assert await read_limited(small, 100) == b"x" * 10
    with pytest.raises(ValueError, match="too large"):
        await read_limited(MagicMock(content=mock_stream(b""), content_length=10_000), 100)
    with pytest.raises(ValueError, match="larger"):
        await read_limited(MagicMock(content=mock_stream(b"x" * 1000), content_length=None), 100)


def test_webcams_on_another_host_are_dropped() -> None:
    result = {
        "webcams": [
            {"name": "ok", "snapshot_url": "/webcam/?action=snapshot", "stream_url": "/webcam/?action=stream"},
            {"name": "evil", "snapshot_url": "http://169.254.169.254/latest", "stream_url": "file:///etc/passwd"},
            {"name": "mixed", "snapshot_url": "http://10.0.0.1/snap", "stream_url": "/webcam2/?action=stream"},
        ]
    }
    cams = parse_webcams(result, "http://192.168.1.95/", frozenset({"192.168.1.95"}))
    assert [c.name for c in cams] == ["ok", "mixed"]
    assert cams[1].snapshot_url is None
    assert cams[1].stream_url == "http://192.168.1.95/webcam2/?action=stream"


async def test_camera_snapshot_follows_no_redirect(hass: HomeAssistant, aioclient_mock) -> None:
    from custom_components.fdm_printer_manager.camera import PrinterCamera  # noqa: PLC0415

    from .test_camera import CENTAURI_CAMS, SNAPSHOT  # noqa: PLC0415

    coordinator = MagicMock()
    coordinator.hass = hass
    coordinator.config_entry.entry_id = "e"
    coordinator.webcam = parse_webcams(CENTAURI_CAMS, "http://192.168.1.95/")[0]
    camera = PrinterCamera.__new__(PrinterCamera)
    camera.coordinator = coordinator
    camera._session = MagicMock()
    response = MagicMock(status=302)
    camera._session.get = MagicMock(
        return_value=MagicMock(__aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock())
    )
    assert await camera.async_camera_image() is None
    assert camera._session.get.call_args.kwargs["allow_redirects"] is False
    assert camera._session.get.call_args.args[0] == SNAPSHOT


# --------------------------------------------------------------------------- #
# Admin only (finding 1)
# --------------------------------------------------------------------------- #
async def test_sheets_setup_and_mark_print_are_admin_only(hass: HomeAssistant, hass_read_only_user) -> None:
    from custom_components.fdm_printer_manager.services import async_setup_services  # noqa: PLC0415

    async_setup_services(hass)
    context = Context(user_id=hass_read_only_user.id)
    with pytest.raises(Unauthorized):
        await hass.services.async_call(DOMAIN, "sheets_setup", {}, blocking=True, context=context, return_response=True)
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN, "mark_print", {"entry_id": "x", "result": "failed"}, blocking=True, context=context
        )


# --------------------------------------------------------------------------- #
# Options flow: secrets never sent back, web_url only http(s) (findings 9, 12)
# --------------------------------------------------------------------------- #
def test_secrets_are_not_suggested() -> None:
    schema = klipper_schema({"api_key": "s3cret", "host": "h"}).schema
    key = next(k for k in schema if str(k) == "api_key")
    assert not (key.description or {}).get("suggested_value")
    schema = bambu_lan_schema({"access_code": "12345678", "host": "h"}, with_serial=False).schema
    code = next(k for k in schema if str(k) == "access_code")
    assert not (code.description or {}).get("suggested_value")
    assert "12345678" not in repr(schema)


async def test_bambu_options_keep_the_code_when_left_empty(hass: HomeAssistant) -> None:
    from homeassistant.data_entry_flow import FlowResultType  # noqa: PLC0415

    from custom_components.fdm_printer_manager.adapters.bambu_lan import BambuStatus  # noqa: PLC0415

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="A1 mini",
        data={
            "printer_type": "bambu_lan",
            "printer_name": "A1 mini",
            "host": "192.168.1.96",
            "printer_serial": "0309DA561105372",
            "access_code": "oldcode1",
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "bambu_lan"
    test = AsyncMock(return_value=BambuStatus("idle"))
    with patch("custom_components.fdm_printer_manager.config_flow.BambuLanClient.async_test", test):
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"host": "192.168.1.96"})
    assert result["step_id"] == "electricity"
    for _ in range(4):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"update_interval": 30, "currency": "EUR", "auto_off_minutes": 15}
            if result["step_id"] == "behaviour"
            else {},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert {**entry.data, **entry.options}["access_code"] == "oldcode1"


async def test_web_url_must_be_http(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Ender 6",
        data={"printer_type": "klipper", "printer_name": "Ender 6", "host": "192.168.1.89", "port": 7125},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"host": "192.168.1.89", "port": 7125, "web_url": "javascript:alert(1)"}
    )
    assert result["errors"] == {"web_url": "invalid_url"}


def test_old_bad_web_url_is_not_used() -> None:
    from custom_components.fdm_printer_manager.coordinator import PrintCostCoordinator  # noqa: PLC0415

    coordinator = PrintCostCoordinator.__new__(PrintCostCoordinator)
    coordinator.printer_type = PrinterType.KLIPPER
    coordinator.conf = {"host": "192.168.1.89", "web_url": "javascript:alert(1)"}
    assert coordinator.configuration_url == "http://192.168.1.89/"
    coordinator.conf = {"host": "192.168.1.89", "web_url": "https://mainsail.local/"}
    assert coordinator.configuration_url == "https://mainsail.local/"


# --------------------------------------------------------------------------- #
# Diagnostics (finding 14)
# --------------------------------------------------------------------------- #
async def test_diagnostics_hide_what_identifies_the_printer(hass: HomeAssistant, machine: Machine) -> None:  # noqa: F811
    entry = await setup(hass, ssh_username="pi", ssh_password="pw", ssh_command="sudo poweroff")
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag["config"])
    for secret in ("192.168.1.89", "pi", "pw", "sudo poweroff"):
        assert f"'{secret}'" not in text


# --------------------------------------------------------------------------- #
# Baby steps like Mainsail / Fluidd (finding 10)
# --------------------------------------------------------------------------- #
def test_babystep_with_unknown_offset_is_sent_like_mainsail() -> None:
    assert babystep_gcode(0.05, None) == "SET_GCODE_OFFSET Z_ADJUST=+0.050 MOVE=1"
    assert babystep_gcode(-0.05, None) == "SET_GCODE_OFFSET Z_ADJUST=-0.050 MOVE=1"
    with pytest.raises(ValueError, match="within"):
        babystep_gcode(0.05, 0.98)  # known offset: the 1 mm cap still applies


# --------------------------------------------------------------------------- #
# Tesseract.js shipped with the integration, frozen (finding 4)
# --------------------------------------------------------------------------- #
def test_vendored_ocr_files_match_their_checksums() -> None:
    ocr = COMPONENT / "frontend" / "ocr"
    listed: dict[str, str] = {}
    for line in (ocr / "SHA256SUMS").read_text("utf-8").splitlines():
        digest, name = line.split("  ", 1)
        listed[name] = digest
    present = {p.relative_to(ocr).as_posix() for p in ocr.rglob("*") if p.is_file() and p.name != "SHA256SUMS"}
    assert present == set(listed)
    for name, digest in listed.items():
        assert hashlib.sha256((ocr / name).read_bytes()).hexdigest() == digest, name
    third_party_md = COMPONENT.parent.parent / "THIRD_PARTY.md"
    if not third_party_md.is_file():  # the Docker test run copies only the integration and the tests
        return
    third_party = third_party_md.read_text("utf-8")
    for name in (
        "tesseract.min.js",
        "worker.min.js",
        "core/tesseract-core-simd-lstm.wasm.js",
        "lang/eng.traineddata.gz",
    ):
        assert listed[name] in third_party


def test_panel_loads_the_text_reader_locally() -> None:
    panel = (COMPONENT / "frontend" / "fdm-printer-panel.js").read_text("utf-8")
    assert "cdn.jsdelivr" not in panel
    assert re.search(r'OCR_BASE = "/fdm_printer_manager_static/ocr"', panel)
    for option in ("workerPath", "corePath", "langPath", "workerBlobURL: false"):
        assert option in panel
    assert 'Tesseract.recognize(blob, "eng", OCR_OPTIONS)' in panel
    for name in ("worker.min.js", "core/tesseract-core-lstm.wasm.js", "lang/eng.traineddata.gz", "tesseract.min.js"):
        assert (COMPONENT / "frontend" / "ocr" / name).is_file()
