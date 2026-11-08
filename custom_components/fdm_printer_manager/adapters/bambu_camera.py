"""Camera of Bambu Lab A1, A1 mini, P1P and P1S printers in LAN mode (TLS, port 6000).

The printer sends one JPEG every second or two, no video. The protocol (the
same one Bambu Studio and ha-bambulab use; see THIRD_PARTY.md):

1. TLS connection to port 6000 (the printer's own certificate, not from a
   public CA: remembered on first contact and compared before logging in).
2. The client sends one 80-byte login packet: four little-endian 32-bit
   words ``0x40, 0x3000, 0, 0``, then the user name ``bblp`` and the access
   code, each padded with zero bytes to 32 bytes.
3. The printer answers with frames: a 16-byte header (bytes 0-3: little-endian
   size of the JPEG that follows; the rest is fixed) and the JPEG itself
   (``FF D8`` ... ``FF D9``). A wrong access code: the printer closes the
   connection without sending anything.

X1, H2 and P2S printers use RTSPS on port 322 instead (it needs "LAN mode
liveview" on the printer): not supported yet.

The connection is opened when someone wants an image and closed again when
nobody has asked for one for :data:`IDLE_CLOSE` seconds. The access code is
never logged.

No Home Assistant import: tested against a fake printer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from collections.abc import Callable, Coroutine
from typing import Any

from .bambu_lan import _tls, cert_fingerprint

_LOGGER = logging.getLogger(__name__)

CAMERA_PORT = 6000
USERNAME = "bblp"
HEADER_SIZE = 16
FIELD_SIZE = 32  # user name and access code are padded to 32 bytes
MAX_FRAME = 4 * 1024 * 1024  # one JPEG (about 100 kB at 1280 x 720); more is not a camera
CONNECT_TIMEOUT = 10  # seconds to open the TLS connection
FRAME_TIMEOUT = 10  # seconds without a frame: the camera stopped (it sends one every 1-2 s)
IDLE_CLOSE = 30  # seconds without anyone asking for an image: close the connection
FRESH = 2.0  # seconds a frame is still "the latest" (the printer sends one every 1-2 s)
RETRY_AFTER = 15  # seconds before trying again after a failed connection

# Models with the JPEG camera on port 6000 (names from naming.BAMBU_SERIAL_MODELS).
JPEG_CAMERA_MODELS = frozenset({"A1", "A1 mini", "P1P", "P1S"})

JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"


class BambuCameraError(Exception):
    """The camera did not answer, closed the connection or sent something that is not a frame."""


class BambuCameraIdentityError(BambuCameraError):
    """The camera's TLS certificate is not the one seen before: the access code is not sent."""


def has_jpeg_camera(model: str | None) -> bool:
    """True for the models whose camera is the JPEG stream on port 6000."""
    return model in JPEG_CAMERA_MODELS


def auth_packet(access_code: str, username: str = USERNAME) -> bytes:
    """The 80-byte login packet. ``ValueError`` for a code that is not ASCII or longer than 32 bytes."""
    user = username.encode("ascii")
    code = access_code.encode("ascii")
    if len(user) > FIELD_SIZE or len(code) > FIELD_SIZE:
        raise ValueError("user name or access code too long")
    return struct.pack("<IIII", 0x40, 0x3000, 0, 0) + user.ljust(FIELD_SIZE, b"\0") + code.ljust(FIELD_SIZE, b"\0")


def frame_size(header: bytes) -> int:
    """Size of the JPEG announced by a 16-byte header (``BambuCameraError`` if it cannot be one)."""
    if len(header) != HEADER_SIZE:
        raise BambuCameraError(f"frame header of {len(header)} bytes")
    (size,) = struct.unpack_from("<I", header)
    if not len(JPEG_START) + len(JPEG_END) <= size <= MAX_FRAME:
        raise BambuCameraError(f"frame size {size} out of range")
    return size


def is_jpeg(data: bytes) -> bool:
    """Starts and ends like a JPEG."""
    return data.startswith(JPEG_START) and data.endswith(JPEG_END)


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    """One frame: header, then the JPEG it announces."""
    size = frame_size(await reader.readexactly(HEADER_SIZE))
    data = await reader.readexactly(size)
    if not is_jpeg(data):
        raise BambuCameraError("frame is not a JPEG")
    return data


class BambuCamera:
    """Images from one printer's camera; the connection lives only while someone is watching."""

    def __init__(
        self,
        host: str,
        access_code: str,
        create_task: Callable[[Coroutine[Any, Any, None]], asyncio.Task],
        *,
        port: int = CAMERA_PORT,
        use_tls: bool = True,
        check_identity: Callable[[str | None], bool] | None = None,
        idle_close: float = IDLE_CLOSE,
        frame_timeout: float = FRAME_TIMEOUT,
        connect_timeout: float = CONNECT_TIMEOUT,
        retry_after: float = RETRY_AFTER,
    ) -> None:
        self.host = host
        self._access_code = access_code
        self._create_task = create_task
        self._port = port
        self._tls = use_tls
        # Called with the TLS certificate's fingerprint before logging in; False: not the same printer.
        self._check_identity = check_identity
        self._idle_close = idle_close
        self._frame_timeout = frame_timeout
        self._connect_timeout = connect_timeout
        self._retry_after = retry_after
        self._task: asyncio.Task | None = None
        self._waiters: list[asyncio.Future[bytes | None]] = []
        self._frame: bytes | None = None
        self._frame_at = 0.0
        self._wanted_at = 0.0
        self._failed_at: float | None = None
        self.connected = False

    def __repr__(self) -> str:
        return f"BambuCamera({self.host}:{self._port})"  # never the access code

    @property
    def streaming(self) -> bool:
        """The connection to the camera is open (or being opened)."""
        return self._task is not None

    async def async_image(self) -> bytes | None:
        """The latest JPEG: a fresh one at once, else the next one (``None`` if the camera does not answer)."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        self._wanted_at = now
        if self._frame is not None and now - self._frame_at <= FRESH:
            return self._frame
        if self._task is None:
            if self._failed_at is not None and now - self._failed_at < self._retry_after:
                return None  # failed a moment ago: do not hammer a printer that is off or refusing
            self._task = self._create_task(self._run())
        waiter: asyncio.Future[bytes | None] = loop.create_future()
        self._waiters.append(waiter)
        try:
            return await asyncio.wait_for(waiter, self._connect_timeout + self._frame_timeout)
        except TimeoutError:
            return None
        finally:
            with contextlib.suppress(ValueError):
                self._waiters.remove(waiter)

    async def stop(self) -> None:
        """Close the connection (integration unloaded)."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._wake(None)

    # ---------------------------------------------------------------- #
    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self._port, ssl=_tls() if self._tls else None),
                self._connect_timeout,
            )
        except (OSError, TimeoutError) as err:
            raise BambuCameraError(f"{self.host}:{self._port}: {err}") from err
        if self._check_identity is not None and not self._check_identity(cert_fingerprint(writer)):
            writer.close()
            raise BambuCameraIdentityError(f"{self.host}: not the same printer (camera TLS certificate changed)")
        try:
            writer.write(auth_packet(self._access_code))
            await writer.drain()
        except (OSError, ValueError) as err:
            writer.close()
            raise BambuCameraError(f"{self.host}: cannot log in to the camera ({type(err).__name__})") from None
        return reader, writer

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await self._open()
            self.connected = True
            first = True
            while loop.time() - self._wanted_at < self._idle_close:
                try:
                    frame = await asyncio.wait_for(read_frame(reader), self._frame_timeout)
                except asyncio.IncompleteReadError as err:
                    if first and not err.partial:
                        raise BambuCameraError("camera closed the connection (wrong access code?)") from None
                    raise BambuCameraError("camera closed the connection") from None
                except TimeoutError:
                    raise BambuCameraError("camera sent no image") from None
                first = False
                self._failed_at = None
                self._frame, self._frame_at = frame, loop.time()
                self._wake(frame)
            _LOGGER.debug("Bambu camera %s: nobody watching, closing", self.host)
        except asyncio.CancelledError:
            raise
        except BambuCameraIdentityError as err:
            _LOGGER.warning("Bambu camera %s: %s; no access code sent", self.host, err)
            self._failed_at = loop.time()
        except (BambuCameraError, OSError) as err:
            _LOGGER.debug("Bambu camera %s: %s", self.host, err)
            self._failed_at = loop.time()
        finally:
            self.connected = False
            if writer is not None:
                writer.close()
            if self._task is asyncio.current_task():
                self._task = None
            self._wake(None)

    def _wake(self, frame: bytes | None) -> None:
        """Hand a frame (or ``None``: no image) to everyone waiting."""
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(frame)
