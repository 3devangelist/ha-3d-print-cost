"""Watching a running print: printer down, print stuck, print interrupted.

Pure logic, no Home Assistant import: the coordinator feeds one
:class:`WatchInput` per reading and acts on the :class:`WatchAction` list.

* **down**: the printer stops answering while a print is open (power cut,
  Wi-Fi, crash). Said once, after ``down_after_s``.
* **back** / **resumed**: it answers again; "resumed" when it is printing.
* **interrupted**: the print is still open but the printer answers and is not
  printing (Klipper comes back in "standby" after a power cut, or a state got
  stuck) for ``close_after_s``: the print is closed as failed. Nothing is
  resumed automatically: restarting a print on a cold, maybe detached part is
  the user's call.
* **stalled**: printing, but the progress has not moved for ``stall_after_s``
  (a warning only: long first layers and big infills do exist).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

DEFAULT_DOWN_AFTER = 120  # seconds unreachable before "down"
DEFAULT_CLOSE_AFTER = 600  # seconds reachable but not printing before the print is closed
DEFAULT_STALL_AFTER = 3600  # seconds without progress before "stalled"


class AlertKind(StrEnum):
    """What happened."""

    DOWN = "down"
    BACK = "back"
    RESUMED = "resumed"
    INTERRUPTED = "interrupted"
    STALLED = "stalled"


@dataclass(slots=True, frozen=True)
class WatchInput:
    """One reading of the printer."""

    now: datetime
    job_open: bool  # the tracker has a print in progress
    reachable: bool  # the printer answers
    printing: bool  # its state is printing / paused / preparing
    progress: float | None = None
    status: str | None = None


@dataclass(slots=True, frozen=True)
class WatchAction:
    """Something the coordinator must do: tell the user, and maybe close the print as failed."""

    kind: AlertKind
    close_job: bool = False
    after_power_loss: bool = False  # "interrupted" after the printer was down
    minutes: int = 0  # how long the condition lasted
    status: str | None = None


class Watchdog:
    """State of the watch over one printer."""

    def __init__(
        self,
        *,
        down_after_s: float = DEFAULT_DOWN_AFTER,
        close_after_s: float = DEFAULT_CLOSE_AFTER,
        stall_after_s: float = DEFAULT_STALL_AFTER,
    ) -> None:
        self.down_after_s = down_after_s
        self.close_after_s = close_after_s
        self.stall_after_s = stall_after_s
        self._reset()

    def _reset(self) -> None:
        self.down_since: datetime | None = None
        self.down_alerted = False
        self.was_down = False  # down during this print and not resumed since
        self.idle_since: datetime | None = None
        self.progress: float | None = None
        self.progress_since: datetime | None = None
        self.stall_alerted = False

    @property
    def alert(self) -> AlertKind | None:
        """Condition shown on the printer card right now."""
        if self.down_alerted:
            return AlertKind.DOWN
        if self.stall_alerted:
            return AlertKind.STALLED
        return None

    def check(self, inp: WatchInput) -> list[WatchAction]:
        """Feed one reading."""
        if not inp.job_open:
            self._reset()
            return []
        if not inp.reachable:
            self.idle_since = None
            self.progress_since = None
            if self.down_since is None:
                self.down_since = inp.now
            down_s = (inp.now - self.down_since).total_seconds()
            if not self.down_alerted and down_s >= self.down_after_s:
                self.down_alerted = True
                self.was_down = True
                return [WatchAction(AlertKind.DOWN, minutes=round(down_s / 60), status=inp.status)]
            return []

        actions: list[WatchAction] = []
        if self.down_since is not None:
            if self.down_alerted:
                actions.append(
                    WatchAction(
                        AlertKind.RESUMED if inp.printing else AlertKind.BACK,
                        minutes=round((inp.now - self.down_since).total_seconds() / 60),
                        status=inp.status,
                    )
                )
            self.down_since = None
            self.down_alerted = False
            if inp.printing:
                self.was_down = False

        if inp.printing:
            self.idle_since = None
            self.was_down = False
            actions.extend(self._check_progress(inp))
            return actions

        # Answers, a print is open, but the printer is not printing.
        self.progress_since = None
        if self.idle_since is None:
            self.idle_since = inp.now
        idle_s = (inp.now - self.idle_since).total_seconds()
        if idle_s >= self.close_after_s:
            actions.append(
                WatchAction(
                    AlertKind.INTERRUPTED,
                    close_job=True,
                    after_power_loss=self.was_down,
                    minutes=round(idle_s / 60),
                    status=inp.status,
                )
            )
            self._reset()
        return actions

    def _check_progress(self, inp: WatchInput) -> list[WatchAction]:
        if inp.progress is None:
            return []
        if self.progress is None or inp.progress != self.progress or self.progress_since is None:
            self.progress = inp.progress
            self.progress_since = inp.now
            self.stall_alerted = False
            return []
        still_s = (inp.now - self.progress_since).total_seconds()
        if not self.stall_alerted and still_s >= self.stall_after_s:
            self.stall_alerted = True
            return [WatchAction(AlertKind.STALLED, minutes=round(still_s / 60), status=inp.status)]
        return []

    def ended_after_power_loss(self) -> bool:
        """The printer ended the print right after being down (a Bambu printer comes back "idle" or "failed")."""
        return self.was_down or self.down_since is not None
