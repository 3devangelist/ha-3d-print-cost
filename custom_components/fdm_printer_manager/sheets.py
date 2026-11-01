"""One row per finished print in a Google Sheet, through Home Assistant's ``google_sheets`` integration.

The row is first queued in the store (``sheets_pending``), then sent with
``google_sheets.append_sheet``. A row that cannot be sent (the integration is not
set up, the spreadsheet or the tab is missing, no network) stays in the queue and
is sent before the next one: at the next finished print, or when Home Assistant
starts. Sending never delays nor breaks the end of a print.

Limits of the ``google_sheets`` integration: it can only write to spreadsheets it
created itself, and the worksheet (tab) must already exist. It adds the time it
writes the row in a ``created`` column, and any column missing from the header row.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, tzinfo
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant

from .const import CONF_SHEETS_ENTRY, CONF_SHEETS_WORKSHEET, DEFAULT_SHEETS_WORKSHEET, DOMAIN

_LOGGER = logging.getLogger(__name__)

SHEETS_DOMAIN = "google_sheets"
SHEETS_SERVICE = "append_sheet"
SEND_TIMEOUT = 60  # seconds for one row
PENDING_LIMIT = 200  # rows kept while Google Sheets cannot be reached; the oldest go first

# Column headers, in order (Google Sheets adds "created" itself).
COLUMNS: tuple[str, ...] = (
    "printer",
    "file",
    "result",
    "result_by",
    "started",
    "finished",
    "duration_min",
    "energy_kwh",
    "electricity_cost",
    "currency",
    "notes",
    "job_id",
)


def _local(value: Any, tz: tzinfo) -> str:
    """ISO time from the summary -> "YYYY-MM-DD HH:MM:SS" in Home Assistant's time zone (Sheets reads it as a date)."""
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if moment.tzinfo is not None:
        moment = moment.astimezone(tz)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _blank(value: Any) -> Any:
    return "" if value is None else value


def sheet_row(payload: dict[str, Any], tz: tzinfo, *, correction: str | None = None) -> dict[str, Any]:
    """The row of one finished print, from its summary (the ``print_finished`` event data).

    ``correction``: the print was marked again by hand; the row repeats the print
    with its new result, and says so in ``notes``.
    """
    notes = payload.get("notes") or []
    if isinstance(notes, str):
        notes = [notes]
    notes = [str(n) for n in notes]
    if correction:
        notes.insert(0, correction)
    duration_s = payload.get("duration_s")
    return {
        "printer": _blank(payload.get("printer")),
        "file": _blank(payload.get("name")),
        "result": payload.get("result") or "unknown",
        "result_by": payload.get("result_by") or "printer",
        "started": _local(payload.get("started_at"), tz),
        "finished": _local(payload.get("finished_at"), tz),
        "duration_min": round(float(duration_s) / 60, 1) if duration_s is not None else "",
        "energy_kwh": _blank(payload.get("energy_kwh")),
        "electricity_cost": _blank(payload.get("electricity_cost")),
        "currency": _blank(payload.get("currency")),
        "notes": "; ".join(notes),
        "job_id": _blank(payload.get("job_id")),
    }


def queue_row(pending: list[dict[str, Any]], row: dict[str, Any]) -> None:
    """Add a row to the queue, dropping the oldest when it is full."""
    pending.append(row)
    if len(pending) > PENDING_LIMIT:
        dropped = len(pending) - PENDING_LIMIT
        del pending[:dropped]
        _LOGGER.warning("Google Sheets: %s unsent print row(s) dropped (more than %s waiting)", dropped, PENDING_LIMIT)


class SheetsLog:
    """Sends the queued rows of one printer, in order, one at a time."""

    def __init__(self, hass: HomeAssistant, entry_id: str, printer_name: str, conf: dict[str, Any]) -> None:
        self._hass = hass
        self._printer = printer_name
        self._notification_id = f"{DOMAIN}_{entry_id}_google_sheets"
        self.config_entry: str | None = conf.get(CONF_SHEETS_ENTRY) or None
        self.worksheet: str = (conf.get(CONF_SHEETS_WORKSHEET) or "").strip() or DEFAULT_SHEETS_WORKSHEET
        self._lock = asyncio.Lock()
        self._failing = False  # a notification is up; shown once until a row goes through

    @property
    def enabled(self) -> bool:
        """A Google Sheets entry was chosen in the options."""
        return self.config_entry is not None

    async def async_flush(self, pending: list[dict[str, Any]], save: Callable[[], Awaitable[None]]) -> None:
        """Send the queued rows, oldest first; stop at the first failure (kept for next time). Never raises."""
        if not self.enabled or not pending:
            return
        async with self._lock:
            sent = 0
            while pending:
                row = pending[0]
                try:
                    async with asyncio.timeout(SEND_TIMEOUT):
                        await self._hass.services.async_call(
                            SHEETS_DOMAIN,
                            SHEETS_SERVICE,
                            {"config_entry": self.config_entry, "worksheet": self.worksheet, "data": row},
                            blocking=True,
                        )
                except Exception as err:  # noqa: BLE001 - the row waits in the queue
                    self._failed(err, len(pending))
                    break
                pending.pop(0)
                sent += 1
            if sent:
                try:
                    await save()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("%s: cannot save the Google Sheets queue: %s", self._printer, err)
                if not pending and self._failing:
                    self._failing = False
                    persistent_notification.async_dismiss(self._hass, self._notification_id)
                _LOGGER.debug("%s: %s print row(s) sent to Google Sheets", self._printer, sent)

    def _failed(self, err: Exception, waiting: int) -> None:
        reason = str(err) or type(err).__name__
        _LOGGER.warning(
            "%s: cannot add the print to Google Sheets (worksheet '%s'), %s row(s) waiting: %s",
            self._printer,
            self.worksheet,
            waiting,
            reason,
        )
        if self._failing:
            return
        self._failing = True
        persistent_notification.async_create(
            self._hass,
            (
                f"The print could not be added to Google Sheets (worksheet **{self.worksheet}**): {reason}\n\n"
                "Check that the Google Sheets integration is set up, that its spreadsheet still exists and "
                f"has a tab named **{self.worksheet}**. The row is kept and sent with the next print "
                "or when Home Assistant restarts."
            ),
            title=f"{self._printer}: Google Sheets",
            notification_id=self._notification_id,
        )
