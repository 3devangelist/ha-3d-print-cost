"""Klipper printers, read directly from Moonraker's HTTP API.

No other integration is needed: the user types the printer's IP address and
this module polls ``/printer/objects/query``. Only ``aiohttp`` (shipped with
Home Assistant) is used, so there is no extra requirement to install.

The parsing helpers are pure functions and unit-tested without a printer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import aiohttp

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 7125
QUERY_OBJECTS = ("print_stats", "virtual_sdcard", "display_status", "extruder")

# Status reported when the printer cannot be reached. The tracker treats it as
# "unknown": a running print stays open until the printer answers again.
STATUS_UNAVAILABLE = "unavailable"


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


@dataclass(slots=True)
class FileMetadata:
    """What the slicer wrote in the G-code header."""

    estimated_time_s: float | None = None


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

    async def _get(self, path: str) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            async with self._session.get(url, headers=self._headers, timeout=self._timeout) as resp:
                if resp.status in (401, 403):
                    raise MoonrakerAuthError(f"HTTP {resp.status} from {url}")
                if resp.status >= 400:
                    raise MoonrakerConnectionError(f"HTTP {resp.status} from {url}")
                data = await resp.json(content_type=None)
        except MoonrakerError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise MoonrakerConnectionError(f"{url}: {err}") from err
        if not isinstance(data, dict) or "result" not in data:
            raise MoonrakerConnectionError(f"{url}: not a Moonraker response")
        return data["result"]

    async def server_info(self) -> dict[str, Any]:
        """Moonraker version and Klipper state."""
        return await self._get("/server/info")

    async def printer_info(self) -> dict[str, Any]:
        """Hostname and Klipper state (fails while Klipper is down)."""
        return await self._get("/printer/info")

    async def status(self) -> KlipperStatus:
        """Current print state, file, progress and nozzle temperature."""
        server = await self.server_info()
        klippy_state = server.get("klippy_state")
        if klippy_state != "ready":
            return parse_status({}, klippy_state)
        result = await self._get("/printer/objects/query?" + "&".join(QUERY_OBJECTS))
        return parse_status(result.get("status") or {}, klippy_state)

    async def metadata(self, filename: str) -> FileMetadata:
        """Slicer metadata of a G-code file."""
        result = await self._get(f"/server/files/metadata?filename={quote(filename)}")
        return parse_metadata(result)

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

    async def async_update(self) -> None:
        """Poll the printer. Never raises: an unreachable printer reads as unavailable."""
        try:
            reading = await self.client.status()
        except MoonrakerError as err:
            if self.available:
                _LOGGER.info("Klipper printer unreachable: %s", err)
            self.available = False
            self.reading = KlipperStatus(state=STATUS_UNAVAILABLE)
            return
        if not self.available:
            _LOGGER.info("Klipper printer reachable (state: %s)", reading.state)
        self.available = True
        self.reading = reading
        if reading.filename and reading.filename != self._meta_for:
            self._meta_for = reading.filename
            try:
                self.meta = await self.client.metadata(reading.filename)
            except MoonrakerError as err:
                _LOGGER.debug("No metadata for %s: %s", reading.filename, err)
                self.meta = None

    @property
    def remaining_min(self) -> float | None:
        """Remaining time of the current print."""
        if self.reading.state not in ("printing", "paused"):
            return None
        return remaining_minutes(self.reading, self.meta if self._meta_for == self.reading.filename else None)
