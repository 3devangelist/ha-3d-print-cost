"""Telling the user what the watchdog saw: a notification, an event and the chosen notify actions."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant

from .const import DOMAIN, EVENT_ALERT, NAME
from .watchdog import AlertKind, WatchAction

_LOGGER = logging.getLogger(__name__)


def parse_notify_services(value: str | None) -> list[str]:
    """``"notify.a, mobile_app_b"`` → ``["a", "mobile_app_b"]`` (service names of the notify domain)."""
    names = []
    for part in (value or "").replace(";", ",").split(","):
        name = part.strip().removeprefix("notify.")
        if name:
            names.append(name)
    return names


def alert_message(
    action: WatchAction, printer: str, job: str | None, progress: float | None, close_minutes: int
) -> tuple[str, str]:
    """Title and text of an alert."""
    what = f"'{job}'" if job else "the print"
    at = f" at {round(progress)}%" if progress is not None else ""
    state = f" (state: {action.status})" if action.status else ""
    kind = action.kind
    if kind is AlertKind.DOWN:
        return (
            f"⚠️ {printer} is not answering",
            f"{printer} stopped answering in the middle of {what}{at}. If it comes back and does not resume "
            f"within {close_minutes} min, the print is closed as failed.",
        )
    if kind is AlertKind.BACK:
        return (
            f"{printer} is back",
            f"{printer} is answering again but is not printing{state}. If it does not resume within "
            f"{close_minutes} min, the print is closed as failed.",
        )
    if kind is AlertKind.RESUMED:
        return (
            f"🔌 {printer} resumed",
            f"{printer} is printing {what} again after {action.minutes} min without answering.",
        )
    if kind is AlertKind.INTERRUPTED:
        if action.after_power_loss:
            text = (
                f"{what.capitalize()} on {printer} was interrupted{at}: the printer lost power or the connection "
                "and did not resume."
            )
        else:
            text = (
                f"{what.capitalize()} was still open, but {printer} has not been printing{state} "
                f"for {action.minutes} min."
            )
        return (
            f"❌ {printer}: print interrupted",
            f"{text} It was closed as failed. Check the part before printing again: nothing is resumed automatically.",
        )
    return (
        f"⏳ {printer}: no progress",
        f"The progress of {what} has not moved{at} for {action.minutes} min. Check the printer.",
    )


class AlertSender:
    """Sends the alerts of one printer."""

    def __init__(self, hass: HomeAssistant, entry_id: str, printer: str, notify_services: str | None) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.printer = printer
        self.notify = parse_notify_services(notify_services)

    def send(self, action: WatchAction, job: str | None, progress: float | None, close_minutes: int) -> None:
        """Notification (replaced by the next alert of this printer), event, notify actions."""
        title, message = alert_message(action, self.printer, job, progress, close_minutes)
        _LOGGER.warning("%s: %s", title, message)
        notification_id = f"{DOMAIN}_watch_{self.entry_id}"
        if action.kind is AlertKind.RESUMED:
            persistent_notification.async_dismiss(self.hass, notification_id)
        else:
            persistent_notification.async_create(
                self.hass, message, title=f"{NAME}: {title}", notification_id=notification_id
            )
        data: dict[str, Any] = {
            "entry_id": self.entry_id,
            "printer": self.printer,
            "kind": str(action.kind),
            "title": title,
            "message": message,
            "job_name": job,
            "progress": progress,
        }
        self.hass.bus.async_fire(EVENT_ALERT, data)
        for service in self.notify:
            if not self.hass.services.has_service("notify", service):
                _LOGGER.warning("%s: notify.%s does not exist", self.printer, service)
                continue
            self.hass.async_create_task(
                self._async_notify(service, title, message), f"{DOMAIN}_notify_{service}", eager_start=False
            )

    async def _async_notify(self, service: str, title: str, message: str) -> None:
        try:
            await self.hass.services.async_call("notify", service, {"title": title, "message": message}, blocking=True)
        except Exception as err:  # noqa: BLE001 - a failing phone must not break the printer
            _LOGGER.warning("%s: notify.%s failed: %s", self.printer, service, err)
