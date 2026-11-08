"""A notification on the phone when a print ends, with a "Keep on" action (0.15.0).

Sent through the notify actions chosen for the printer (Edit → When a print ends)
or, when the printer has none, those of "FDM Printers" (its options). None chosen:
nothing is sent. With the Home Assistant mobile app, the notification has a
"Keep on" button while the printer is going to turn off by itself; tapping it
fires ``mobile_app_notification_action`` with the action
``FDM_KEEP_ON_<entry_id>``, and the coordinator cancels that auto-off.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant

from .alerts import parse_notify_services
from .const import (
    CONF_FINISH_NOTIFY,
    CONF_FINISH_NOTIFY_SERVICES,
    CONF_PANEL,
    DOMAIN,
    KEEP_ON_ACTION,
)

_LOGGER = logging.getLogger(__name__)

_RESULT_TITLE = {
    "success": ("✅", "print finished"),
    "cancelled": ("⏹️", "print cancelled"),
    "failed": ("❌", "print failed"),
}


def panel_options(hass: HomeAssistant) -> dict[str, Any]:
    """Options of the "FDM Printers" entry (shared by every printer), ``{}`` when there is none."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_PANEL):
            return dict(entry.options)
    return {}


def finish_targets(conf: dict[str, Any], shared: dict[str, Any]) -> list[str]:
    """Notify actions for the end of a print: the printer's, else the shared ones; ``[]`` when turned off."""
    if not conf.get(CONF_FINISH_NOTIFY, True):
        return []
    return parse_notify_services(conf.get(CONF_FINISH_NOTIFY_SERVICES)) or parse_notify_services(
        shared.get(CONF_FINISH_NOTIFY_SERVICES)
    )


def duration_text(seconds: float | None) -> str:
    """``7500`` → ``2 h 05 min``; ``2100`` → ``35 min``."""
    if seconds is None:
        return "?"
    minutes = round(seconds / 60)
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest:02d} min" if hours else f"{rest} min"


def finish_message(
    printer: str,
    name: str | None,
    result: str | None,
    duration_s: float | None,
    *,
    off_at: datetime | None,
    off_pending: bool,
    cool_to: int,
    why_on: str | None,
) -> tuple[str, str]:
    """Title and text: printer, file, result, duration, and when it turns itself off (or why it stays on).

    ``off_at`` is already in local time.
    """
    icon, what = _RESULT_TITLE.get(result or "", ("❔", "print ended"))
    lines = [f"{name or 'Print'} · {result or 'unknown'} · {duration_text(duration_s)}"]
    if off_pending and off_at is not None:
        lines.append(f"Turns off by itself at {off_at:%H:%M} (nozzle below {cool_to} °C first). Keep on: leave it on.")
    elif off_pending:
        lines.append(f"Turns off by itself once the nozzle is below {cool_to} °C. Keep on: leave it on.")
    elif why_on:
        lines.append(why_on)
    return f"{icon} {printer}: {what}", "\n".join(lines)


def keep_on_action(entry_id: str) -> str:
    """Action id of the notification's "Keep on" button."""
    return f"{KEEP_ON_ACTION}{entry_id}"


async def async_send_finish(
    hass: HomeAssistant,
    services: list[str],
    *,
    entry_id: str,
    printer: str,
    title: str,
    message: str,
    keep_on: bool,
) -> None:
    """Send to every chosen notify action. A phone that fails never breaks the printer."""
    data: dict[str, Any] = {"tag": f"{DOMAIN}_finish_{entry_id}", "group": DOMAIN}
    if keep_on:
        data["actions"] = [{"action": keep_on_action(entry_id), "title": "Keep on"}]
    for service in services:
        if not hass.services.has_service("notify", service):
            _LOGGER.warning("%s: notify.%s does not exist", printer, service)
            continue
        try:
            await hass.services.async_call(
                "notify", service, {"title": title, "message": message, "data": data}, blocking=True
            )
        except Exception as err:  # noqa: BLE001 - a failing phone must not break the printer
            _LOGGER.warning("%s: notify.%s failed: %s", printer, service, err)
