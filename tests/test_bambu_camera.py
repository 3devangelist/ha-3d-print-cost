"""Bambu Lab LAN camera (A1 / A1 mini / P1, port 6000), against a fake printer."""

import asyncio
import datetime
import hashlib
import ssl
import struct
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.components.camera import async_get_image
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fdm_printer_manager.adapters.bambu_camera import (
    MAX_FRAME,
    BambuCamera,
    BambuCameraError,
    auth_packet,
    frame_size,
    has_jpeg_camera,
    is_jpeg,
    read_frame,
)
from custom_components.fdm_printer_manager.const import DOMAIN

CODE = "12345678"
JPEG_1 = b"\xff\xd8\xff\xe0" + b"frame-one" * 50 + b"\xff\xd9"
JPEG_2 = b"\xff\xd8\xff\xe0" + b"frame-two" * 80 + b"\xff\xd9"
A1_MINI = "0309CA123456789"
X1C = "00M00A123456789"


def header(size: int) -> bytes:
    """16-byte frame header as the printer sends it."""
    return struct.pack("<IIII", size, 0, 1, 0)


# --------------------------------------------------------------------------- #
# Packets (pure functions)
# --------------------------------------------------------------------------- #
def test_auth_packet_layout() -> None:
    packet = auth_packet(CODE)
    assert len(packet) == 80
    assert packet[:16] == bytes([0x40, 0, 0, 0, 0, 0x30, 0, 0]) + bytes(8)
    assert packet[16:48] == b"bblp" + bytes(28)
    assert packet[48:80] == CODE.encode() + bytes(24)


def test_auth_packet_refuses_what_cannot_be_sent() -> None:
    with pytest.raises(ValueError, match="too long"):
        auth_packet("x" * 33)
    with pytest.raises(UnicodeEncodeError):
        auth_packet("código")


def test_frame_header_and_jpeg_checks() -> None:
    assert frame_size(header(len(JPEG_1))) == len(JPEG_1)
    with pytest.raises(BambuCameraError):
        frame_size(header(MAX_FRAME + 1))  # announces more than any JPEG: not a camera
    with pytest.raises(BambuCameraError):
        frame_size(header(0))
    with pytest.raises(BambuCameraError):
        frame_size(b"\x00" * 15)
    assert is_jpeg(JPEG_1)
    assert not is_jpeg(b"not a jpeg")


async def _reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def test_read_frame() -> None:
    assert await read_frame(await _reader(header(len(JPEG_1)) + JPEG_1)) == JPEG_1
    with pytest.raises(BambuCameraError, match="not a JPEG"):
        await read_frame(await _reader(header(6) + b"abcdef"))
    with pytest.raises(asyncio.IncompleteReadError):
        await read_frame(await _reader(header(len(JPEG_1)) + JPEG_1[:10]))


def test_models_with_the_port_6000_camera() -> None:
    assert all(has_jpeg_camera(m) for m in ("A1", "A1 mini", "P1P", "P1S"))
    assert not any(has_jpeg_camera(m) for m in ("X1 Carbon", "X1", "X1E", "H2D", None))


# --------------------------------------------------------------------------- #
# Fake printer
# --------------------------------------------------------------------------- #
class FakePrinter:
    """Port 6000 of an A1 mini: checks the login, then sends a JPEG every ``interval`` seconds."""

    def __init__(
        self, frames: list[bytes] | None = None, interval: float = 0.05, code: str = CODE, max_frames: int = 0
    ) -> None:
        self.frames = frames or [JPEG_1, JPEG_2]
        self.max_frames = max_frames  # 0: no limit; else stop sending (but keep the connection) after that many
        self.interval = interval
        self.code = code
        self.logins: list[bytes] = []
        self.open = 0
        self.closed = asyncio.Event()
        self.raw: bytes | None = None  # sent as is instead of frames

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.open += 1
        try:
            login = await asyncio.wait_for(reader.readexactly(80), 2)
            self.logins.append(login)
            if login != auth_packet(self.code):
                return  # the real printer just hangs up
            if self.raw is not None:
                writer.write(self.raw)
                await writer.drain()
                await asyncio.sleep(1)
                return
            i = 0
            while not self.max_frames or i < self.max_frames:
                frame = self.frames[i % len(self.frames)]
                writer.write(header(len(frame)))  # the header always comes on its own
                await writer.drain()
                writer.write(frame)
                await writer.drain()
                i += 1
                await asyncio.sleep(self.interval)
            await reader.read()  # silent until the client hangs up
        except (OSError, asyncio.IncompleteReadError, TimeoutError):
            pass
        finally:
            self.open -= 1
            writer.close()
            self.closed.set()


async def _serve(printer: FakePrinter, ssl_context: ssl.SSLContext | None = None):
    server = await asyncio.start_server(printer.handle, "127.0.0.1", 0, ssl=ssl_context)
    return server, server.sockets[0].getsockname()[1]


def _camera(port: int, code: str = CODE, **kwargs) -> BambuCamera:
    loop = asyncio.get_running_loop()
    return BambuCamera("127.0.0.1", code, loop.create_task, port=port, use_tls=False, **kwargs)


@pytest.mark.usefixtures("socket_enabled")
async def test_image_from_the_fake_printer() -> None:
    printer = FakePrinter()
    server, port = await _serve(printer)
    async with server:
        camera = _camera(port)
        first = await camera.async_image()
        assert first == JPEG_1
        assert printer.logins == [auth_packet(CODE)]
        # A frame of the last 2 s is answered at once, on the same connection.
        assert await camera.async_image() == JPEG_1
        await asyncio.sleep(0.2)
        assert camera.connected
        assert await camera.async_image() in (JPEG_1, JPEG_2)
        assert len(printer.logins) == 1  # one connection for everything
        await camera.stop()
        await asyncio.wait_for(printer.closed.wait(), 2)
        assert not camera.streaming


@pytest.mark.usefixtures("socket_enabled")
async def test_connection_closed_when_nobody_watches() -> None:
    printer = FakePrinter()
    server, port = await _serve(printer)
    async with server:
        camera = _camera(port, idle_close=0.3)
        assert await camera.async_image() == JPEG_1
        await asyncio.wait_for(printer.closed.wait(), 3)
        assert printer.open == 0
        assert not camera.streaming
        assert not camera.connected
        # Someone looks again: a new connection.
        printer.closed.clear()
        await asyncio.sleep(2.1)  # the cached frame is no longer fresh
        assert await camera.async_image() is not None
        assert len(printer.logins) == 2
        await camera.stop()


@pytest.mark.usefixtures("socket_enabled")
async def test_wrong_access_code_gives_no_image_and_no_retry_storm() -> None:
    printer = FakePrinter(code="87654321")
    server, port = await _serve(printer)
    async with server:
        camera = _camera(port)
        assert await camera.async_image() is None
        assert await camera.async_image() is None  # failed a moment ago: not tried again yet
        assert len(printer.logins) == 1
        await camera.stop()


@pytest.mark.usefixtures("socket_enabled")
async def test_garbage_is_not_an_image() -> None:
    printer = FakePrinter()
    printer.raw = header(20) + b"not a jpeg, twenty b"
    server, port = await _serve(printer)
    async with server:
        assert await _camera(port).async_image() is None
    printer.raw = header(MAX_FRAME * 4)  # announces 16 MB: refused before reading it
    server, port = await _serve(printer)
    async with server:
        assert await _camera(port).async_image() is None


@pytest.mark.usefixtures("socket_enabled")
async def test_camera_that_stops_sending_is_closed() -> None:
    printer = FakePrinter(max_frames=1)
    server, port = await _serve(printer)
    async with server:
        camera = _camera(port, frame_timeout=0.3)
        assert await camera.async_image() == JPEG_1
        await asyncio.wait_for(printer.closed.wait(), 3)  # no frame within 0.3 s: the camera hangs up
        assert not camera.streaming


@pytest.mark.usefixtures("socket_enabled")
async def test_printer_off() -> None:
    camera = BambuCamera(
        "127.0.0.1", CODE, asyncio.get_running_loop().create_task, port=1, use_tls=False, connect_timeout=1
    )
    assert await camera.async_image() is None
    assert CODE not in repr(camera)


@pytest.mark.usefixtures("socket_enabled")
async def test_identity_refused_before_the_access_code_is_sent() -> None:
    received: list[bytes] = []

    async def handle(reader, writer):
        received.append(await reader.read(100))
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        camera = _camera(port, check_identity=lambda fp: False)
        assert await camera.async_image() is None
        await asyncio.sleep(0.1)
    assert CODE.encode() not in b"".join(received)


def _self_signed(tmp_path: Path) -> tuple[ssl.SSLContext, str]:
    """A TLS server context with a new self-signed certificate (like the printer's), and its SHA-256."""
    from cryptography import x509  # noqa: PLC0415
    from cryptography.hazmat.primitives import hashes, serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import ec  # noqa: PLC0415
    from cryptography.x509.oid import NameOID  # noqa: PLC0415

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "0309CA123456789")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)
    fingerprint = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    return ctx, fingerprint


@pytest.mark.usefixtures("socket_enabled")
async def test_over_tls_with_the_certificate_checked(tmp_path: Path) -> None:
    ctx, fingerprint = _self_signed(tmp_path)
    printer = FakePrinter()
    server, port = await _serve(printer, ctx)
    seen: list[str | None] = []

    def check(fp: str | None) -> bool:
        seen.append(fp)
        return fp == fingerprint

    async with server:
        loop = asyncio.get_running_loop()
        camera = BambuCamera("127.0.0.1", CODE, loop.create_task, port=port, check_identity=check)
        assert await camera.async_image() == JPEG_1
        assert seen == [fingerprint]
        await camera.stop()

        other = BambuCamera("127.0.0.1", CODE, loop.create_task, port=port, check_identity=lambda fp: False)
        assert await other.async_image() is None
        await asyncio.sleep(0.1)
    assert len(printer.logins) == 1  # the access code went only to the known certificate


# --------------------------------------------------------------------------- #
# camera.<printer>_camera
# --------------------------------------------------------------------------- #
def _entry(serial: str = A1_MINI, name: str = "Bambu Lab A1 mini") -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=name,
        unique_id=f"bambu_{serial}",
        data={
            "printer_type": "bambu_lan",
            "printer_name": name,
            "host": "192.168.1.96",
            "printer_serial": serial,
            "access_code": CODE,
        },
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    with patch("custom_components.fdm_printer_manager.coordinator.BambuLanClient.start"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_a1_mini_gets_a_camera_shown_on_its_card(hass: HomeAssistant) -> None:
    entry = _entry()
    await _setup(hass, entry)
    coordinator = entry.runtime_data
    state = hass.states.get("camera.bambu_lab_a1_mini_camera")
    assert state is not None
    assert state.state == "unavailable"  # MQTT not connected yet: the camera is not even tried
    assert CODE not in str(state.attributes)
    status = hass.states.get("sensor.bambu_lab_a1_mini_printer_status")
    assert status.attributes["camera_entity"] == "camera.bambu_lab_a1_mini_camera"

    coordinator.bambu_lan.available = True
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get("camera.bambu_lab_a1_mini_camera").state != "unavailable"
    with patch.object(coordinator.bambu_camera, "async_image", return_value=JPEG_2) as image:
        assert (await async_get_image(hass, "camera.bambu_lab_a1_mini_camera")).content == JPEG_2
    image.assert_awaited_once()

    # Not the known printer any more: no image, the access code is not sent.
    coordinator.identity.refuse("tls_cert", "other")
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    with (
        patch.object(coordinator.bambu_camera, "async_image") as image,
        pytest.raises(HomeAssistantError),
    ):
        await async_get_image(hass, "camera.bambu_lab_a1_mini_camera")
    image.assert_not_called()

    with patch.object(coordinator.bambu_camera, "stop") as stop:
        assert await hass.config_entries.async_unload(entry.entry_id)
    stop.assert_awaited_once()


async def test_camera_certificate_is_remembered_then_checked(hass: HomeAssistant) -> None:
    entry = _entry()
    await _setup(hass, entry)
    coordinator = entry.runtime_data
    assert coordinator._check_bambu_camera_identity("aaaa")
    assert coordinator.identity.known["camera_tls_cert"] == "aaaa"
    assert coordinator._check_bambu_camera_identity("aaaa")
    assert not coordinator._check_bambu_camera_identity("bbbb")
    assert coordinator.identity.changed
    await hass.config_entries.async_unload(entry.entry_id)


async def test_x1_has_no_camera_yet(hass: HomeAssistant) -> None:
    """X1 / H2 use RTSP on port 322: not supported yet, so no entity that would never show anything."""
    entry = _entry(X1C, "Bambu Lab X1 Carbon")
    await _setup(hass, entry)
    assert entry.runtime_data.bambu_camera is None
    cameras = [e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id) if e.domain == "camera"]
    assert cameras == []
    assert hass.states.get("sensor.bambu_lab_x1_carbon_printer_status").attributes["camera_entity"] is None
    await hass.config_entries.async_unload(entry.entry_id)
