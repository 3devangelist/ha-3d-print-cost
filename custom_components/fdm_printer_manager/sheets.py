"""One row per finished print in a Google Sheet, through Home Assistant's ``google_sheets`` integration.

The row is first queued in the store (``sheets_pending``), then sent with
``google_sheets.append_sheet``. A row that cannot be sent (the integration is not
set up, the spreadsheet or the tab is missing, no network) stays in the queue and
is sent before the next one: at the next finished print, or when Home Assistant
starts. Sending never delays nor breaks the end of a print.

Limits of the ``google_sheets`` integration: it can only write to spreadsheets it
created itself, and the worksheet (tab) must already exist. It adds the time it
writes the row in a ``created`` column, and any column missing from the header row.

Marking a print again by hand (0.12.3): the row already in the sheet is changed in
place (its ``result`` and ``result_by`` cells, found by ``job_id``), through the
Google login of the ``google_sheets`` entry. A row still waiting in the queue is
simply changed there. When the row cannot be changed (not found, no access), a
correction row is added instead, as before, and kept in the queue until it goes
through.

Setting up (the panel's "Google Sheets" button, or the ``sheets_setup`` action):
``async_setup_log`` creates the tab with its header row when it is missing, using
the Google login of that ``google_sheets`` entry (the same gspread client the core
integration uses), then chooses that entry and tab in every printer's options.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, tzinfo
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import CONF_PANEL, CONF_SHEETS_ENTRY, CONF_SHEETS_WORKSHEET, DEFAULT_SHEETS_WORKSHEET, DOMAIN

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
# Header row written in a new tab ("created" first, as google_sheets fills it in).
HEADER: tuple[str, ...] = ("created", *COLUMNS)
NEW_TAB_ROWS = 1000
# Marks a queued row that corrects a row already sent: change that row in place, else append this one.
CORRECTION_KEY = "_correction"
# The cells a correction changes.
CORRECTED: tuple[str, ...] = ("result", "result_by")
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/{}"


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


def correction_note(result: str, before: str | None) -> str:
    """The ``notes`` text of a correction row."""
    return f"Correction: marked as {result} by hand (was {before or 'unknown'})"


def mark_pending_row(pending: list[dict[str, Any]], job_id: str, result: str) -> bool:
    """A row of this print still waits in the queue: change it there (newest first). ``False``: none waits."""
    for row in reversed(pending):
        if str(row.get("job_id") or "") != str(job_id):
            continue
        before = row.get("result")
        row["result"] = result
        row["result_by"] = "user"
        if row.get(CORRECTION_KEY):
            notes = str(row.get("notes") or "")
            was = notes.split("(was ", 1)[1].split(")", 1)[0] if "(was " in notes else before
            rest = notes.split(")", 1)[1].lstrip("; ") if notes.startswith("Correction:") and ")" in notes else ""
            row["notes"] = "; ".join(n for n in (correction_note(result, was), rest) if n)
        return True
    return False


def _column_letters(index: int) -> str:
    """1 -> A, 27 -> AA."""
    letters = ""
    while index > 0:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def update_rows(access_token: str, spreadsheet_id: str, title: str, job_id: str, values: dict[str, Any]) -> int:
    """Change the cells of every row of ``job_id`` in the tab. Runs in the executor. Returns the rows changed.

    Columns are found by the header row, so a tab whose columns were moved still works.
    """
    from google.oauth2.credentials import Credentials  # noqa: PLC0415
    from gspread import Client  # noqa: PLC0415

    worksheet = Client(Credentials(access_token)).open_by_key(spreadsheet_id).worksheet(title)
    header = [str(h).strip() for h in worksheet.row_values(1)]
    if "job_id" not in header:
        return 0
    columns = {key: header.index(key) + 1 for key in values if key in header}
    if not columns:
        return 0
    ids = worksheet.col_values(header.index("job_id") + 1)
    rows = [number for number, value in enumerate(ids, start=1) if number > 1 and str(value).strip() == str(job_id)]
    if not rows:
        return 0
    worksheet.batch_update(
        [
            {"range": f"{_column_letters(column)}{row}", "values": [[values[key]]]}
            for row in rows
            for key, column in columns.items()
        ],
        value_input_option="RAW",
    )
    return len(rows)


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
                if row.get(CORRECTION_KEY) and await self._async_update_in_place(row):
                    pending.pop(0)
                    sent += 1
                    continue
                data = {key: value for key, value in row.items() if key != CORRECTION_KEY}
                try:
                    async with asyncio.timeout(SEND_TIMEOUT):
                        await self._hass.services.async_call(
                            SHEETS_DOMAIN,
                            SHEETS_SERVICE,
                            {"config_entry": self.config_entry, "worksheet": self.worksheet, "data": data},
                            blocking=True,
                        )
                except Exception as err:  # noqa: BLE001 - the row waits in the queue
                    self._failed(err, len(pending))
                    break
                if any(row.get(key) != data.get(key) for key in CORRECTED):
                    # Marked by hand while it was being sent: the sheet has the old result, correct it next.
                    row[CORRECTION_KEY] = True
                    row["notes"] = correction_note(str(row.get("result")), str(data.get("result")))
                    continue
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

    async def _async_update_in_place(self, row: dict[str, Any]) -> bool:
        """Change the print's row already in the sheet. ``False`` (never raises): append a correction row."""
        job_id = str(row.get("job_id") or "")
        entry = self._hass.config_entries.async_get_entry(self.config_entry) if self.config_entry else None
        if not job_id or entry is None or entry.domain != SHEETS_DOMAIN or entry.state is not ConfigEntryState.LOADED:
            return False
        values = {key: row.get(key) for key in CORRECTED}
        try:
            async with asyncio.timeout(SEND_TIMEOUT):
                await entry.runtime_data.async_ensure_token_valid()
                token = entry.data[CONF_TOKEN][CONF_ACCESS_TOKEN]
                changed = await self._hass.async_add_executor_job(
                    update_rows, token, entry.unique_id, self.worksheet, job_id, values
                )
        except Exception as err:  # noqa: BLE001 - fall back to a correction row
            _LOGGER.info(
                "%s: cannot change the row of print %s in Google Sheets (%s), adding a correction row",
                self._printer,
                job_id,
                err or type(err).__name__,
            )
            return False
        if not changed:
            _LOGGER.info("%s: print %s not found in Google Sheets, adding a correction row", self._printer, job_id)
            return False
        _LOGGER.debug("%s: print %s changed in Google Sheets (%s row(s))", self._printer, job_id, changed)
        return True

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


# --------------------------------------------------------------------------- #
# Setting up: one button in the panel
# --------------------------------------------------------------------------- #
def spreadsheet_url(entry: ConfigEntry) -> str:
    """Link to the spreadsheet of a ``google_sheets`` entry (its unique ID is the spreadsheet ID)."""
    return SPREADSHEET_URL.format(entry.unique_id)


def printer_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """The printers of this integration (not the sidebar page entry)."""
    return [e for e in hass.config_entries.async_entries(DOMAIN) if not e.data.get(CONF_PANEL)]


def _printer_conf(entry: ConfigEntry) -> dict[str, Any]:
    return {**entry.data, **entry.options}


def sheets_status(hass: HomeAssistant) -> dict[str, Any]:
    """The Google Sheets entries that can be chosen, and where each printer logs now."""
    entries = [
        {
            "entry_id": e.entry_id,
            "title": e.title,
            "loaded": e.state is ConfigEntryState.LOADED,
            "url": spreadsheet_url(e),
        }
        for e in hass.config_entries.async_entries(SHEETS_DOMAIN)
    ]
    printers = []
    for entry in printer_entries(hass):
        conf = _printer_conf(entry)
        chosen = conf.get(CONF_SHEETS_ENTRY) or None
        worksheet = (conf.get(CONF_SHEETS_WORKSHEET) or "").strip() or DEFAULT_SHEETS_WORKSHEET
        printers.append(
            {
                "entry_id": entry.entry_id,
                "title": entry.title,
                "sheets_entry": chosen,
                "worksheet": worksheet if chosen else None,
            }
        )
    return {"entries": entries, "printers": printers}


def prepare_worksheet(access_token: str, spreadsheet_id: str, title: str) -> dict[str, Any]:
    """Open the spreadsheet; add the tab (with its header row) if it is missing. Runs in the executor.

    gspread and google-auth are installed with Home Assistant's ``google_sheets``
    integration, so they are imported only here, once that integration is set up.
    """
    from google.oauth2.credentials import Credentials  # noqa: PLC0415
    from gspread import Client  # noqa: PLC0415
    from gspread.exceptions import WorksheetNotFound  # noqa: PLC0415

    sheet = Client(Credentials(access_token)).open_by_key(spreadsheet_id)
    created = False
    try:
        worksheet = sheet.worksheet(title)
    except WorksheetNotFound:
        worksheet = sheet.add_worksheet(title=title, rows=NEW_TAB_ROWS, cols=len(HEADER))
        created = True
    header = False
    if created or not worksheet.row_values(1):
        worksheet.append_row(list(HEADER), value_input_option="RAW")
        header = True
        with contextlib.suppress(Exception):  # only cosmetic: keep the header row in view
            worksheet.freeze(rows=1)
    return {"spreadsheet_title": sheet.title, "created": created, "header": header, "gid": worksheet.id}


def _sheets_entry(hass: HomeAssistant, entry_id: str | None) -> ConfigEntry:
    entries = hass.config_entries.async_entries(SHEETS_DOMAIN)
    if not entries:
        raise ServiceValidationError(
            "Google Sheets is not set up in Home Assistant. Add the Google Sheets integration first "
            "(it creates the spreadsheet), then try again."
        )
    if not entry_id:
        return entries[0]
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != SHEETS_DOMAIN:
        raise ServiceValidationError(f"{entry_id} is not a Google Sheets integration entry")
    return entry


def _targets(hass: HomeAssistant, printer_ids: list[str] | None) -> list[ConfigEntry]:
    printers = printer_entries(hass)
    if printer_ids:
        printers = [e for e in printers if e.entry_id in printer_ids]
        if missing := set(printer_ids) - {e.entry_id for e in printers}:
            raise ServiceValidationError(f"Not a printer of {DOMAIN}: {', '.join(sorted(missing))}")
    if not printers:
        raise ServiceValidationError("There is no printer to log yet. Add a printer first.")
    return printers


async def async_setup_log(
    hass: HomeAssistant,
    sheets_entry_id: str | None = None,
    worksheet: str | None = None,
    printer_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Log the printers (all of them, or ``printer_ids``) to a tab of a Google Sheets entry.

    Creates the tab when it is missing, then sets the entry and the tab in each
    printer's options, keeping every other option. Returns where the rows go.
    """
    entry = _sheets_entry(hass, sheets_entry_id)
    title = (worksheet or "").strip() or DEFAULT_SHEETS_WORKSHEET
    targets = _targets(hass, printer_ids)
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            f"The Google Sheets entry '{entry.title}' is not working ({entry.state.value}). "
            "Open Settings, Devices and services, Google Sheets, and sign in with Google again."
        )
    try:
        await entry.runtime_data.async_ensure_token_valid()
        token = entry.data[CONF_TOKEN][CONF_ACCESS_TOKEN]
        found = await hass.async_add_executor_job(prepare_worksheet, token, entry.unique_id, title)
    except Exception as err:  # gspread / Google errors: say what failed, change nothing
        if type(err).__name__ == "RefreshError":
            entry.async_start_reauth(hass)
        raise HomeAssistantError(
            f"Cannot open the spreadsheet of '{entry.title}' or add the tab '{title}': {err or type(err).__name__}"
        ) from err

    changed = []
    for printer in targets:
        options = {**printer.options, CONF_SHEETS_ENTRY: entry.entry_id, CONF_SHEETS_WORKSHEET: title}
        if options != dict(printer.options):
            hass.config_entries.async_update_entry(printer, options=options)
            changed.append(printer.title)
    url = spreadsheet_url(entry)
    _LOGGER.info(
        "Google Sheets: %s log to '%s' > %s%s",
        ", ".join(p.title for p in targets),
        entry.title,
        title,
        " (tab created)" if found["created"] else "",
    )
    return {
        "config_entry": entry.entry_id,
        "title": entry.title,
        "spreadsheet_title": found["spreadsheet_title"],
        "worksheet": title,
        "created": found["created"],
        "header": found["header"],
        "url": url,
        "worksheet_url": f"{url}/edit#gid={found['gid']}",
        "printers": [p.title for p in targets],
        "changed": changed,
    }


async def async_stop_log(hass: HomeAssistant, printer_ids: list[str] | None = None) -> list[str]:
    """Stop logging to Google Sheets (all printers, or ``printer_ids``); the tab name is kept."""
    stopped = []
    for printer in printer_entries(hass):
        if printer_ids and printer.entry_id not in printer_ids:
            continue
        if not _printer_conf(printer).get(CONF_SHEETS_ENTRY):
            continue
        hass.config_entries.async_update_entry(printer, options={**printer.options, CONF_SHEETS_ENTRY: None})
        stopped.append(printer.title)
    return stopped
