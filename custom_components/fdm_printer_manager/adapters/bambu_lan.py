"""Bambu Lab printers over the local network (LAN mode), no other integration needed.

The printer runs an MQTT broker on port 8883 (TLS, self-signed certificate).
User ``bblp``, password = the access code shown on the printer's screen.
It publishes its state on ``device/<serial>/report`` and takes commands on
``device/<serial>/request``. A1 / P1 printers only send what changed, so the
reports are merged and a full report (``pushall``) is requested regularly.

A tiny MQTT 3.1.1 client is included (connect, subscribe, publish QoS 0,
ping): it is all we need and avoids an extra dependency.

Printers also announce themselves with SSDP NOTIFY messages on UDP port 2021
(some firmwares 1990), not the usual 1900; :func:`parse_ssdp` reads them for
discovery.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import ssl
import struct
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger(__name__)

MQTT_PORT = 8883
SSDP_PORTS = (2021, 1990)  # A1 / P1 broadcast on 2021; some firmwares use 1990
SSDP_MULTICAST = "239.255.255.250"
KEEPALIVE = 60
PUSHALL_EVERY = 300  # seconds
STATUS_UNAVAILABLE = "unavailable"


class BambuError(Exception):
    """Base error."""


class BambuConnectionError(BambuError):
    """Printer unreachable or not answering MQTT."""


class BambuAuthError(BambuError):
    """Access code refused."""


# --------------------------------------------------------------------------- #
# MQTT packets (pure functions, unit-tested)
# --------------------------------------------------------------------------- #
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n % 128
        n //= 128
        if n:
            byte |= 0x80
        out.append(byte)
        if not n:
            return bytes(out)


def _str(s: str) -> bytes:
    data = s.encode()
    return struct.pack("!H", len(data)) + data


def packet_connect(client_id: str, username: str, password: str, keepalive: int = KEEPALIVE) -> bytes:
    """CONNECT with username/password and a clean session."""
    var = _str("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", keepalive)
    payload = _str(client_id) + _str(username) + _str(password)
    body = var + payload
    return bytes([0x10]) + _varint(len(body)) + body


def packet_subscribe(packet_id: int, topic: str) -> bytes:
    """SUBSCRIBE to one topic at QoS 0."""
    body = struct.pack("!H", packet_id) + _str(topic) + b"\x00"
    return bytes([0x82]) + _varint(len(body)) + body


def packet_publish(topic: str, payload: bytes) -> bytes:
    """PUBLISH at QoS 0."""
    body = _str(topic) + payload
    return bytes([0x30]) + _varint(len(body)) + body


PACKET_PING = bytes([0xC0, 0x00])


async def read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """One MQTT packet: (type nibble, body)."""
    first = (await reader.readexactly(1))[0]
    mult, length = 1, 0
    while True:
        byte = (await reader.readexactly(1))[0]
        length += (byte & 0x7F) * mult
        if not byte & 0x80:
            break
        mult *= 128
    body = await reader.readexactly(length) if length else b""
    return first, body


def parse_publish(first: int, body: bytes) -> tuple[str, bytes]:
    """Topic and payload of a PUBLISH packet."""
    (tlen,) = struct.unpack("!H", body[:2])
    topic = body[2 : 2 + tlen].decode(errors="replace")
    pos = 2 + tlen
    if (first >> 1) & 0x03:  # QoS > 0 carries a packet id
        pos += 2
    return topic, body[pos:]


# --------------------------------------------------------------------------- #
# Printer state
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class BambuStatus:
    """What the panel and the tracker need."""

    state: str
    filename: str | None = None
    progress: float | None = None
    remaining_min: float | None = None
    nozzle_temp: float | None = None


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def parse_report(merged: dict[str, Any]) -> BambuStatus:
    """Merged ``print`` object → reading. gcode_state: IDLE / PREPARE / RUNNING / PAUSE / FINISH / FAILED."""
    state = str(merged.get("gcode_state") or "").strip().lower() or "idle"
    name = merged.get("subtask_name") or merged.get("gcode_file") or None
    return BambuStatus(
        state=state,
        filename=name or None,
        progress=_num(merged.get("mc_percent")),
        remaining_min=_num(merged.get("mc_remaining_time")),
        nozzle_temp=_num(merged.get("nozzle_temper")),
    )


def command(action: str) -> bytes:
    """Request payload for pause / resume / stop."""
    return json.dumps({"print": {"sequence_id": "0", "command": action, "param": ""}}).encode()


PUSHALL = json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}).encode()


def _tls() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # the printer's certificate is self-signed
    return ctx


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class BambuLanClient:
    """Keeps an MQTT connection to one printer, reconnecting forever."""

    def __init__(
        self,
        host: str,
        serial: str,
        access_code: str,
        on_update: Callable[[], None],
        *,
        port: int = MQTT_PORT,
        use_tls: bool = True,
    ) -> None:
        self.host = host
        self.serial = serial
        self.access_code = access_code
        self._on_update = on_update
        self._port = port
        self._tls = use_tls
        self._merged: dict[str, Any] = {}
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self.available = False
        self.auth_failed = False

    @property
    def reading(self) -> BambuStatus:
        """Latest state (``unavailable`` while disconnected)."""
        if not self.available:
            return BambuStatus(state=STATUS_UNAVAILABLE)
        return parse_report(self._merged)

    # ---------------------------------------------------------------- #
    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self._port, ssl=_tls() if self._tls else None), 10
            )
        except (OSError, TimeoutError) as err:
            raise BambuConnectionError(f"{self.host}:{self._port}: {err}") from err
        writer.write(packet_connect(f"fdm-{self.serial[-6:]}", "bblp", self.access_code))
        await writer.drain()
        try:
            first, body = await asyncio.wait_for(read_packet(reader), 10)
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as err:
            writer.close()
            raise BambuConnectionError(f"{self.host}: no answer to MQTT connect") from err
        if first >> 4 != 2 or len(body) < 2:
            writer.close()
            raise BambuConnectionError(f"{self.host}: not an MQTT broker")
        if body[1] in (4, 5):
            writer.close()
            raise BambuAuthError("access code refused")
        if body[1] != 0:
            writer.close()
            raise BambuConnectionError(f"{self.host}: connection refused (code {body[1]})")
        writer.write(packet_subscribe(1, f"device/{self.serial}/report"))
        writer.write(packet_publish(f"device/{self.serial}/request", PUSHALL))
        await writer.drain()
        return reader, writer

    async def async_test(self, timeout: float = 15) -> BambuStatus:
        """Connect, wait for the first report, disconnect. For the config flow."""
        reader, writer = await self._open()
        try:
            end = asyncio.get_running_loop().time() + timeout
            while True:
                left = end - asyncio.get_running_loop().time()
                if left <= 0:
                    raise BambuConnectionError("connected, but the printer sent no report (check the serial number)")
                first, body = await asyncio.wait_for(read_packet(reader), left)
                if first >> 4 == 3 and self._absorb(*parse_publish(first, body)):
                    return parse_report(self._merged)
        except TimeoutError as err:
            raise BambuConnectionError("connected, but the printer sent no report (check the serial number)") from err
        finally:
            writer.close()

    def start(self, create_task: Callable[..., asyncio.Task]) -> None:
        """Run the connection loop in the background."""
        self._task = create_task(self._run())

    async def stop(self) -> None:
        """Disconnect."""
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self._writer:
            self._writer.close()

    async def _run(self) -> None:
        delay = 5
        while True:
            try:
                reader, writer = await self._open()
                self._writer = writer
                self.auth_failed = False
                delay = 5
                await self._loop(reader, writer)
            except asyncio.CancelledError:
                raise
            except BambuAuthError:
                self.auth_failed = True
                _LOGGER.warning("Bambu %s: access code refused", self.host)
                delay = 300
            except (BambuError, OSError, asyncio.IncompleteReadError, TimeoutError) as err:
                _LOGGER.debug("Bambu %s: %s", self.host, err)
            if self.available:
                self.available = False
                self._on_update()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _loop(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        loop = asyncio.get_running_loop()
        last_ping = last_push = loop.time()
        while True:
            try:
                first, body = await asyncio.wait_for(read_packet(reader), 30)
            except TimeoutError:
                first, body = 0, b""
            now = loop.time()
            if first >> 4 == 3 and self._absorb(*parse_publish(first, body)):
                if not self.available:
                    self.available = True
                self._on_update()
            if now - last_ping > KEEPALIVE / 2:
                writer.write(PACKET_PING)
                last_ping = now
            if now - last_push > PUSHALL_EVERY:
                writer.write(packet_publish(f"device/{self.serial}/request", PUSHALL))
                last_push = now
            await writer.drain()

    def _absorb(self, topic: str, payload: bytes) -> bool:
        """Merge a report into the state. True when it carried print data."""
        if not topic.endswith("/report"):
            return False
        try:
            data = json.loads(payload)
        except ValueError:
            return False
        part = data.get("print") if isinstance(data, dict) else None
        if not isinstance(part, dict):
            return False
        self._merged.update(part)
        return True

    async def async_command(self, action: str) -> None:
        """Pause / resume / stop."""
        if not self._writer or not self.available:
            raise BambuConnectionError("printer not connected")
        self._writer.write(packet_publish(f"device/{self.serial}/request", command(action)))
        await self._writer.drain()


# --------------------------------------------------------------------------- #
# Discovery (SSDP on UDP 2021)
# --------------------------------------------------------------------------- #
@dataclass(slots=True, frozen=True)
class BambuAnnouncement:
    """A printer seen on the network."""

    host: str
    serial: str
    name: str | None
    model: str | None


def parse_ssdp(data: bytes, sender: str) -> BambuAnnouncement | None:
    """Read a Bambu SSDP NOTIFY. ``None`` for anything else."""
    try:
        text = data.decode(errors="replace")
    except Exception:  # noqa: BLE001
        return None
    headers: dict[str, str] = {}
    for line in text.split("\r\n")[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    if "bambulab" not in headers.get("nt", "").lower() and "bambulab" not in headers.get("st", "").lower():
        return None
    serial = headers.get("usn")
    if not serial:
        return None
    return BambuAnnouncement(
        host=headers.get("location") or sender,
        serial=serial,
        name=headers.get("devname.bambu.com"),
        model=headers.get("devmodel.bambu.com"),
    )


class _SsdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_found: Callable[[BambuAnnouncement], None]) -> None:
        self._on_found = on_found

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        found = parse_ssdp(data, addr[0])
        if found:
            self._on_found(found)


async def async_listen_ssdp(
    on_found: Callable[[BambuAnnouncement], None], ports: tuple[int, ...] = SSDP_PORTS
) -> list[asyncio.BaseTransport]:
    """Listen for Bambu announcements. Ports that cannot be opened are skipped (logged)."""
    transports: list[asyncio.BaseTransport] = []
    loop = asyncio.get_running_loop()
    for port in ports:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        except Exception as err:  # noqa: BLE001 - sockets may be unavailable (tests, sandboxes)
            _LOGGER.warning("Bambu discovery off: %s", err)
            return transports
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                with contextlib.suppress(OSError):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.bind(("", port))
            with contextlib.suppress(OSError):
                mreq = socket.inet_aton(SSDP_MULTICAST) + socket.inet_aton("0.0.0.0")
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.setblocking(False)
        except OSError as err:
            sock.close()
            _LOGGER.warning("Bambu discovery: cannot listen on UDP %s (%s)", port, err)
            continue
        transport, _ = await loop.create_datagram_endpoint(lambda: _SsdpProtocol(on_found), sock=sock)
        transports.append(transport)
    return transports
