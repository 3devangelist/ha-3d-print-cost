"""Read-only adapter for HaspelSync.

HaspelSync (https://github.com/Rdiger-36/HaspelSync) owns the AMS slot <->
Spoolman spool mapping and books the *real* consumption of every print in
Spoolman, computed from the sliced ``.gcode.3mf`` (cancelled prints are
pro-rated per layer). This adapter only *reads* what it already computed; it
never calls an action endpoint.

Endpoints used (checked against ``src/openapi.js`` of HaspelSync 1.3.x):

* ``GET /api/printers``             -> ``[{id, name}]`` (id = serial number)
* ``GET /api/status/{printerId}``   -> connection state, ``gcodeState``...
* ``GET /api/spools/{printerId}``   -> one ``ClientSpool`` per slot
* ``GET /api/print/{printerId}``    -> ``PrintState`` incl. ``lastPrintSummary``

Auth: ``Authorization: Bearer <api key>`` (keys are created in HaspelSync's
web UI). The key is never logged.

The ``parse_*`` functions are pure and unit-tested with recorded payloads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from ..models import FilamentUsage, PrintResult, UsageSource

_LOGGER = logging.getLogger(__name__)


class HaspelsyncError(Exception):
    """HaspelSync unreachable or answered something unusable."""


class HaspelsyncAuthError(HaspelsyncError):
    """HaspelSync refused the API key (HTTP 401)."""


class HaspelsyncHostRefusedError(HaspelsyncError):
    """HaspelSync refused the host name it was reached under (HTTP 403).

    From 1.3.0 on, only IPs, localhost, .local names and names listed under
    "Allowed host names" in its settings are accepted.
    """


@dataclass(slots=True, frozen=True)
class HaspelsyncPrinter:
    """A printer known to HaspelSync."""

    printer_id: str
    name: str


@dataclass(slots=True, frozen=True)
class SlotSpool:
    """What sits in one AMS / external slot."""

    slot: str  # A1, B4, HT-A, External, External-2...
    spool_id: int | None
    material: str | None = None
    color: str | None = None
    name: str | None = None
    vendor: str | None = None


@dataclass(slots=True)
class HaspelsyncUsage:
    """Consumption HaspelSync booked for one print."""

    usages: list[FilamentUsage]
    result: PrintResult | None
    job_name: str | None
    ended_at: datetime | None
    note: str | None = None
    unbooked: list[str] = field(default_factory=list)


_STATE_TO_RESULT = {
    "FINISH": PrintResult.SUCCESS,
    "FAILED": PrintResult.FAILED,
    "CANCEL": PrintResult.CANCELLED,
}

# Rows HaspelSync wrote in ``lastPrintSummary.rows``:
#   booked / ambiguous -> booked on ``spoolId``
#   failed             -> grams known, Spoolman refused the booking
#   pending            -> external holder waiting for a spool to be named
#   skipped            -> grams known, no spool could be matched
#   unused             -> filament defined but not printed
_ROWS_WITH_SPOOL = {"booked", "ambiguous", "failed"}
_ROWS_WITHOUT_SPOOL = {"pending", "skipped"}


# --------------------------------------------------------------------------- #
# Pure parsing
# --------------------------------------------------------------------------- #
def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _hex(color: Any) -> str | None:
    if not isinstance(color, str) or not color:
        return None
    color = color.lstrip("#")
    return f"#{color[:6].upper()}"


def _ms_to_dt(value: Any) -> datetime | None:
    ms = _float_or_none(value)
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def parse_printers(payload: Any) -> list[HaspelsyncPrinter]:
    """Parse ``GET /api/printers``."""
    if not isinstance(payload, list):
        raise HaspelsyncError("printer list is not a list")
    return [
        HaspelsyncPrinter(printer_id=str(p["id"]), name=str(p.get("name") or p["id"]))
        for p in payload
        if isinstance(p, dict) and p.get("id")
    ]


def parse_slots(payload: Any) -> dict[str, SlotSpool]:
    """Parse ``GET /api/spools/{id}`` (or ``loadedSpools``) into slot -> spool."""
    slots: dict[str, SlotSpool] = {}
    if not isinstance(payload, list):
        return slots
    for item in payload:
        if not isinstance(item, dict) or not item.get("amsId"):
            continue
        existing = item.get("existingSpool") or {}
        filament = existing.get("filament") or {}
        vendor = (filament.get("vendor") or {}).get("name") if isinstance(filament, dict) else None
        slot_raw = item.get("slot") or {}
        slots[str(item["amsId"])] = SlotSpool(
            slot=str(item["amsId"]),
            spool_id=_int_or_none(item.get("spoolmanId") or existing.get("id")),
            material=item.get("material") or filament.get("material") or slot_raw.get("tray_type"),
            color=_hex(filament.get("color_hex") or slot_raw.get("tray_color")),
            name=item.get("filamentName") or filament.get("name"),
            vendor=item.get("vendor") or vendor,
        )
    return slots


def parse_print_usage(
    payload: Any,
    *,
    job_name: str | None,
    started_at: datetime,
    tolerance: timedelta = timedelta(minutes=2),
) -> HaspelsyncUsage | None:
    """Extract the booked usage of *our* print from ``GET /api/print/{id}``.

    Returns ``None`` when HaspelSync has not (yet) closed a print matching the
    job: wrong name, ended before our job started, or not booked yet. The
    caller retries a few times, then falls back.
    """
    if not isinstance(payload, dict):
        return None
    summary = payload.get("lastPrintSummary")
    if not isinstance(summary, dict):
        return None
    if not payload.get("consumptionBooked", False) and not summary.get("rows"):
        return None

    ended_at = _ms_to_dt(summary.get("endedAt"))
    if ended_at is not None and ended_at < started_at - tolerance:
        return None  # summary of an older print
    summary_name = summary.get("jobName")
    if job_name and summary_name and summary_name != job_name:
        return None

    usages: list[FilamentUsage] = []
    unbooked: list[str] = []
    for row in summary.get("rows") or []:
        if not isinstance(row, dict):
            continue
        status = row.get("status")
        grams = _float_or_none(row.get("grams")) or 0.0
        if status == "unused" or grams <= 0:
            continue
        slot = row.get("amsId")
        if status in _ROWS_WITH_SPOOL:
            spool_id = _int_or_none(row.get("spoolId"))
        elif status in _ROWS_WITHOUT_SPOOL:
            spool_id = None
            unbooked.append(f"{slot or '?'}: {status}")
        else:
            continue
        usages.append(
            FilamentUsage(
                weight_g=grams,
                source=UsageSource.HASPELSYNC,
                slot=slot,
                spool_id=spool_id,
                material=row.get("material") or row.get("type"),
                color=_hex(row.get("color")),
                name=row.get("spoolName"),
                vendor=row.get("vendor"),
                note=row.get("note"),
            )
        )

    return HaspelsyncUsage(
        usages=usages,
        result=_STATE_TO_RESULT.get(str(summary.get("state") or "").upper()),
        job_name=summary_name,
        ended_at=ended_at,
        note=summary.get("note"),
        unbooked=unbooked,
    )


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #
class HaspelsyncClient:
    """Async, read-only HaspelSync API client."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        api_key: str | None,
        *,
        timeout: float = 10.0,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    @property
    def base_url(self) -> str:
        """Base URL (no key in it)."""
        return self._base_url

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    async def _get(self, path: str) -> Any:
        url = f"{self._base_url}{path}"
        try:
            async with self._session.get(url, headers=self._headers(), timeout=self._timeout) as resp:
                if resp.status == 401:
                    raise HaspelsyncAuthError(f"{path}: API key refused")
                if resp.status == 403:
                    raise HaspelsyncHostRefusedError(f"{path}: host name refused")
                if resp.status != 200:
                    raise HaspelsyncError(f"{path}: HTTP {resp.status}")
                return await resp.json(content_type=None)
        except HaspelsyncError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise HaspelsyncError(f"{path}: {type(err).__name__}") from err

    # -- API ----------------------------------------------------------------
    async def get_printers(self) -> list[HaspelsyncPrinter]:
        """All printers HaspelSync monitors."""
        return parse_printers(await self._get("/api/printers"))

    async def get_printer(self, printer_id: str) -> dict[str, Any]:
        """Connection status of one printer."""
        data = await self._get(f"/api/status/{printer_id}")
        if not isinstance(data, dict):
            raise HaspelsyncError("status is not an object")
        return data

    async def get_current_slots(self, printer_id: str) -> dict[str, SlotSpool]:
        """Slot -> spool mapping right now."""
        return parse_slots(await self._get(f"/api/spools/{printer_id}"))

    async def get_print_state(self, printer_id: str) -> dict[str, Any]:
        """Raw running / last print state."""
        data = await self._get(f"/api/print/{printer_id}")
        if not isinstance(data, dict):
            raise HaspelsyncError("print state is not an object")
        return data

    async def get_print_usage(
        self, printer_id: str, *, job_name: str | None, started_at: datetime
    ) -> HaspelsyncUsage | None:
        """Booked usage of the print that started at ``started_at``."""
        return parse_print_usage(await self.get_print_state(printer_id), job_name=job_name, started_at=started_at)
