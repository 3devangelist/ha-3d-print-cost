"""Klipper printers, read directly from Moonraker's HTTP API.

No other integration is needed: the user types the printer's IP address and
this module polls ``/printer/objects/query``. Only ``aiohttp`` (shipped with
Home Assistant) is used, so there is no extra requirement to install.

The parsing helpers are pure functions and unit-tested without a printer.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import aiohttp

from ..identity import klipper_machine_id
from .klipper_extras import KlipperFeatures, discover

_LOGGER = logging.getLogger(__name__)

SPOOLMAN_EVERY = 60  # seconds between reads of Spoolman's active spool

DEFAULT_PORT = 7125
QUERY_OBJECTS = ("print_stats", "virtual_sdcard", "display_status", "extruder")

# Status reported when the printer cannot be reached. The tracker treats it as
# "unknown": a running print stays open until the printer answers again.
STATUS_UNAVAILABLE = "unavailable"

# Largest Moonraker answer read (the biggest real ones, the G-code command list
# and file metadata, are tens of kB): something bigger is not a printer.
MAX_RESPONSE = 2 * 1024 * 1024


class MoonrakerError(Exception):
    """Base error."""


class MoonrakerConnectionError(MoonrakerError):
    """Host unreachable, timeout, or not a Moonraker server."""


class MoonrakerAuthError(MoonrakerError):
    """Moonraker refused the request (API key missing or wrong)."""


@dataclass(slots=True)
class KlipperStatus:
    """One reading of a Klipper printer."""

    state: str
    filename: str | None = None
    progress: float | None = None  # percent, 0-100
    print_duration_s: float | None = None
    nozzle_temp: float | None = None
    klippy_state: str | None = None
    # Raw status of the extra objects (lights, gcode_move, AFC, mmu), read by klipper_extras / filament.
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FileMetadata:
    """What the slicer wrote in the G-code header."""

    estimated_time_s: float | None = None


@dataclass(frozen=True, slots=True)
class Webcam:
    """A camera Moonraker knows about (crowsnest, mjpg-streamer, ...), with absolute URLs."""

    name: str
    snapshot_url: str | None
    stream_url: str | None  # only when it is an MJPEG stream an <img> / HA can relay as is


# Webcam services whose stream URL is a plain MJPEG stream.
MJPEG_SERVICES = ("mjpegstreamer", "mjpegstreamer-adaptive", "uv4l-mjpeg", "ipstream")


def url_host(url: str | None) -> str | None:
    """Host name of an http(s) URL, lower case; ``None`` for anything else."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return parts.hostname.lower()


def _same_host(url: str, hosts: frozenset[str] | None) -> str | None:
    """``url`` when it is http(s) on one of ``hosts`` (``None``: any host), else ``None``."""
    host = url_host(url)
    if host is None or (hosts is not None and host not in hosts):
        _LOGGER.warning("Ignoring a webcam URL on another host: %s", url)
        return None
    return url


def parse_webcams(result: dict[str, Any], web_url: str, hosts: frozenset[str] | None = None) -> list[Webcam]:
    """``server.webcams.list`` → the enabled cameras, URLs made absolute.

    Webcam URLs are usually relative to the printer's web interface
    (``/webcam/?action=stream``), not to Moonraker's port, so they are
    resolved against ``web_url`` (``http://<host>/``). A URL that points to
    another host than ``hosts`` (the printer and its web interface) is dropped:
    Home Assistant must not fetch addresses a printer hands it.
    """
    cams: list[Webcam] = []
    for cam in result.get("webcams") or []:
        if not isinstance(cam, dict) or cam.get("enabled") is False:
            continue
        snapshot = str(cam.get("snapshot_url") or "").strip()
        stream = str(cam.get("stream_url") or "").strip()
        if not snapshot and not stream:
            continue
        service = str(cam.get("service") or "mjpegstreamer").lower()
        snapshot_url = _same_host(urljoin(web_url, snapshot), hosts) if snapshot else None
        stream_url = _same_host(urljoin(web_url, stream), hosts) if stream and service in MJPEG_SERVICES else None
        if not snapshot_url and not stream_url:
            continue
        cams.append(Webcam(name=str(cam.get("name") or "Camera"), snapshot_url=snapshot_url, stream_url=stream_url))
    return cams


def default_web_url(host: str) -> str:
    """Mainsail / Fluidd usually answer on port 80 of the same host."""
    host = host.strip().rstrip("/")
    if host.startswith(("http://", "https://")):
        return f"{host}/"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # bare IPv6 address
    return f"http://{host}/"


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def parse_status(status: dict[str, Any], klippy_state: str | None = None) -> KlipperStatus:
    """Turn the ``result.status`` object of ``printer.objects.query`` into a reading.

    Progress prefers ``virtual_sdcard`` (file position, what Mainsail shows)
    and falls back to ``display_status`` (M73 from the slicer).
    """
    stats = status.get("print_stats") or {}
    state = str(stats.get("state") or "").strip().lower() or "standby"
    if klippy_state and klippy_state != "ready" and state in ("", "standby"):
        # Klipper is in shutdown / error: report it so the tracker can close the job.
        state = "error" if klippy_state == "shutdown" else state

    raw_progress = _num((status.get("virtual_sdcard") or {}).get("progress"))
    if raw_progress is None:
        raw_progress = _num((status.get("display_status") or {}).get("progress"))
    progress = round(max(0.0, min(1.0, raw_progress)) * 100, 1) if raw_progress is not None else None

    return KlipperStatus(
        state=state,
        filename=stats.get("filename") or None,
        progress=progress,
        print_duration_s=_num(stats.get("print_duration")),
        nozzle_temp=_num((status.get("extruder") or {}).get("temperature")),
        klippy_state=klippy_state,
    )


def parse_metadata(meta: dict[str, Any]) -> FileMetadata:
    """``server.files.metadata`` → the slicer's estimated print time."""
    return FileMetadata(estimated_time_s=_num(meta.get("estimated_time")))


def remaining_minutes(reading: KlipperStatus, meta: FileMetadata | None) -> float | None:
    """Slicer estimate minus time spent printing; else extrapolate from progress."""
    spent = reading.print_duration_s
    if spent is None:
        return None
    if meta is not None and meta.estimated_time_s:
        return max(0.0, (meta.estimated_time_s - spent) / 60)
    if reading.progress and reading.progress > 1:
        total = spent / (reading.progress / 100)
        return max(0.0, (total - spent) / 60)
    return None


async def read_limited(resp: Any, limit: int) -> bytes:
    """The body of an aiohttp response, refusing more than ``limit`` bytes."""
    length = getattr(resp, "content_length", None)
    if length is not None and length > limit:
        raise ValueError(f"answer too large ({length} bytes)")
    data = bytearray()
    async for chunk in resp.content.iter_chunked(64 * 1024):
        data += chunk
        if len(data) > limit:
            raise ValueError(f"answer larger than {limit} bytes")
    return bytes(data)


class MoonrakerClient:
    """Minimal async client for the few endpoints we need."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int = DEFAULT_PORT,
        api_key: str | None = None,
        *,
        timeout: float = 10,
    ) -> None:
        self._session = session
        host = host.strip().rstrip("/")
        for prefix in ("http://", "https://"):
            if host.startswith(prefix):
                host = host[len(prefix) :]
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        self.base_url = f"http://{host}:{int(port)}"
        self._headers = {"X-Api-Key": api_key} if api_key else {}
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        # Last ``machine.system_info`` read by machine_identity (firmware.py reads its distribution).
        self.system_info: dict[str, Any] | None = None

    async def _get(self, path: str) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            async with self._session.get(url, headers=self._headers, timeout=self._timeout) as resp:
                if resp.status in (401, 403):
                    raise MoonrakerAuthError(f"HTTP {resp.status} from {url}")
                if resp.status >= 400:
                    raise MoonrakerConnectionError(f"HTTP {resp.status} from {url}")
                data = json.loads(await read_limited(resp, MAX_RESPONSE))
        except MoonrakerError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise MoonrakerConnectionError(f"{url}: {err}") from err
        if not isinstance(data, dict) or "result" not in data:
            raise MoonrakerConnectionError(f"{url}: not a Moonraker response")
        return data["result"]

    async def _post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        # Not "json": that name would hide the json module used to read the reply below.
        url = f"{self.base_url}{path}"
        try:
            async with self._session.post(url, headers=self._headers, json=payload, timeout=self._timeout) as resp:
                if resp.status in (401, 403):
                    raise MoonrakerAuthError(f"HTTP {resp.status} from {url}")
                if resp.status >= 400:
                    # Keep Moonraker's reason, e.g. "Error running shell command: 'sudo systemctl poweroff'"
                    detail = (await read_limited(resp, MAX_RESPONSE)).decode(errors="replace")[:300]
                    raise MoonrakerConnectionError(f"HTTP {resp.status} from {url}: {detail}")
                body = await read_limited(resp, MAX_RESPONSE)
                return (json.loads(body) if body.strip() else None) or {}
        except MoonrakerError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise MoonrakerConnectionError(f"{url}: {err}") from err

    async def pause(self) -> None:
        """Pause the running print."""
        await self._post("/printer/print/pause")

    async def resume(self) -> None:
        """Resume a paused print."""
        await self._post("/printer/print/resume")

    async def cancel(self) -> None:
        """Cancel the running or paused print."""
        await self._post("/printer/print/cancel")

    async def shutdown_host(self) -> None:
        """Shut the printer's computer down (Moonraker ``machine.shutdown``)."""
        await self._post("/machine/shutdown")

    async def server_info(self) -> dict[str, Any]:
        """Moonraker version and Klipper state."""
        return await self._get("/server/info")

    async def printer_info(self) -> dict[str, Any]:
        """Hostname and Klipper state (fails while Klipper is down)."""
        return await self._get("/printer/info")

    async def status(self, extra_objects: tuple[str, ...] = ()) -> KlipperStatus:
        """Current print state, file, progress and nozzle temperature (plus the raw extra objects)."""
        server = await self.server_info()
        klippy_state = server.get("klippy_state")
        if klippy_state != "ready":
            return parse_status({}, klippy_state)
        objects = (*QUERY_OBJECTS, *extra_objects)
        result = await self._get("/printer/objects/query?" + "&".join(quote(o) for o in objects))
        status = result.get("status") or {}
        reading = parse_status(status, klippy_state)
        reading.extras = {k: v for k, v in status.items() if k in extra_objects}
        return reading

    async def features(self) -> KlipperFeatures:
        """Lights, baby steps and filament units this printer offers (read-only requests)."""
        objects = (await self._get("/printer/objects/list")).get("objects") or []
        commands = list(await self._get("/printer/gcode/help"))
        components = (await self.server_info()).get("components") or []
        return discover(objects, commands, components)

    async def gcode(self, script: str) -> None:
        """Run one G-code command (lights, baby steps)."""
        await self._post(f"/printer/gcode/script?script={quote(script)}")

    async def active_spool(self) -> dict[str, Any] | None:
        """Spoolman's active spool, through Moonraker's proxy. ``None`` when none is set."""
        spool_id = (await self._get("/server/spoolman/spool_id")).get("spool_id")
        if spool_id is None:
            return None
        data = await self._post(
            "/server/spoolman/proxy", {"request_method": "GET", "path": f"/v1/spool/{int(spool_id)}"}
        )
        result = data.get("result", data)
        return result if isinstance(result, dict) else None

    async def metadata(self, filename: str) -> FileMetadata:
        """Slicer metadata of a G-code file."""
        result = await self._get(f"/server/files/metadata?filename={quote(filename)}")
        return parse_metadata(result)

    async def machine_identity(self) -> str | None:
        """Who this computer is (identity.py): CPU serial, else MAC addresses, else host name."""
        try:
            info = (await self._get("/machine/system_info")).get("system_info")
        except MoonrakerConnectionError:
            info = None  # older or reduced Moonraker (Elegoo "cosmos"): the host name only
        if isinstance(info, dict):
            self.system_info = info
        return klipper_machine_id(info if isinstance(info, dict) else None, await self.hostname())

    async def webcams(self, web_url: str, hosts: frozenset[str] | None = None) -> list[Webcam]:
        """Enabled cameras. An older Moonraker without the webcam API has none."""
        try:
            result = await self._get("/server/webcams/list")
        except MoonrakerConnectionError as err:
            if "HTTP 404" in str(err):
                return []
            raise
        return parse_webcams(result, web_url, hosts)

    async def hostname(self) -> str | None:
        """Hostname of the printer, used as default name. ``None`` if Klipper is down."""
        try:
            info = await self.printer_info()
        except MoonrakerConnectionError:
            return None
        return info.get("hostname") or None


class KlipperSource:
    """Keeps the latest reading of one Klipper printer (polled by the coordinator)."""

    def __init__(self, client: MoonrakerClient) -> None:
        self.client = client
        self.reading = KlipperStatus(state=STATUS_UNAVAILABLE)
        self.meta: FileMetadata | None = None
        self._meta_for: str | None = None
        self.available = False
        # Lights, baby steps, filament unit: read when the printer is first reached (and again after an outage).
        self.features: KlipperFeatures | None = None
        self._features_due = True
        self._features_retry = 0.0
        self.spool: dict[str, Any] | None = None
        self._spool_read = 0.0

    async def async_update(self) -> None:
        """Poll the printer. Never raises: an unreachable printer reads as unavailable."""
        try:
            if self._features_due or (self.features is None and time.monotonic() >= self._features_retry):
                await self._async_read_features()
            extra = self.features.extra_objects if self.features else ()
            reading = await (self.client.status(extra) if extra else self.client.status())
        except MoonrakerError as err:
            if self.available:
                _LOGGER.info("Klipper printer unreachable: %s", err)
            self.available = False
            self._features_due = True
            self.reading = KlipperStatus(state=STATUS_UNAVAILABLE)
            return
        if not self.available:
            _LOGGER.info("Klipper printer reachable (state: %s)", reading.state)
        self.available = True
        self.reading = reading
        if self.features is not None and self.features.spoolman and not self.features.has_filament_system:
            await self._async_read_spool()
        if reading.filename and reading.filename != self._meta_for:
            self._meta_for = reading.filename
            try:
                self.meta = await self.client.metadata(reading.filename)
            except MoonrakerError as err:
                _LOGGER.debug("No metadata for %s: %s", reading.filename, err)
                self.meta = None

    async def _async_read_features(self) -> None:
        """What the printer offers. Klipper still starting (or an old Moonraker) is retried a minute later."""
        self._features_due = False
        try:
            self.features = await self.client.features()
        except Exception as err:  # noqa: BLE001 - the extras must never stop the printer being read
            _LOGGER.debug("Klipper extras not readable yet: %s", err)
            self.features = None
            self._features_retry = time.monotonic() + 60

    async def _async_read_spool(self) -> None:
        now = time.monotonic()
        if now - self._spool_read < SPOOLMAN_EVERY:
            return
        self._spool_read = now
        try:
            self.spool = await self.client.active_spool()
        except MoonrakerError as err:
            _LOGGER.debug("No active spool: %s", err)
            self.spool = None

    @property
    def remaining_min(self) -> float | None:
        """Remaining time of the current print."""
        if self.reading.state not in ("printing", "paused"):
            return None
        return remaining_minutes(self.reading, self.meta if self._meta_for == self.reading.filename else None)
