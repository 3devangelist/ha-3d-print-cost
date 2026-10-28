"""Print life-cycle state machine.

Receives :class:`PrinterSnapshot` objects (built by the coordinator from Home
Assistant states) and decides when a print starts, is updated, or ends.

No Home Assistant import: the whole life cycle — including restart recovery and
duplicate-state protection — is unit-tested with plain Python objects.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .const import IntegrationMethod
from .models import ElectricityAccumulator, ElectricitySample, JobPhase, PrintJob, PrintResult

_LOGGER = logging.getLogger(__name__)


class PrinterActivity(StrEnum):
    """Coarse meaning of a raw ``print_status`` value."""

    ACTIVE = "active"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    IDLE = "idle"
    UNKNOWN = "unknown"  # offline, unavailable, init, slicing... -> no transition


# Raw values: ha-bambulab's ``print_status`` (the printer's ``gcode_state``
# lower-cased, plus "offline") and Moonraker's ``print_stats.state``.
_ACTIVE = {"prepare", "running", "pause", "paused", "printing"}
_SUCCESS = {"finish", "finished", "complete", "completed", "success"}
_FAILED = {"failed", "failure", "error"}
_CANCELLED = {"cancel", "canceled", "cancelled"}
_IDLE = {"idle"}


def classify_status(raw: str | None) -> PrinterActivity:
    """Map a raw print status to an activity."""
    value = (raw or "").strip().lower()
    if value in _ACTIVE:
        return PrinterActivity.ACTIVE
    if value in _SUCCESS:
        return PrinterActivity.SUCCESS
    if value in _FAILED:
        return PrinterActivity.FAILED
    if value in _CANCELLED:
        return PrinterActivity.CANCELLED
    if value in _IDLE:
        return PrinterActivity.IDLE
    return PrinterActivity.UNKNOWN


_RESULT_FOR_ACTIVITY = {
    PrinterActivity.SUCCESS: PrintResult.SUCCESS,
    PrinterActivity.FAILED: PrintResult.FAILED,
    PrinterActivity.CANCELLED: PrintResult.CANCELLED,
    PrinterActivity.IDLE: PrintResult.UNKNOWN,
}


@dataclass(slots=True)
class PrinterSnapshot:
    """Everything the tracker needs at one instant."""

    timestamp: datetime
    status: str | None
    electricity: ElectricitySample
    job_name: str | None = None
    progress: float | None = None
    remaining_min: float | None = None


class TransitionKind(StrEnum):
    """What a snapshot did to the tracker."""

    STARTED = "started"
    UPDATED = "updated"
    ENDED = "ended"


@dataclass(slots=True, frozen=True)
class Transition:
    """A life-cycle transition produced by :meth:`PrintTracker.process`."""

    kind: TransitionKind
    job: PrintJob


class PrintTracker:
    """Owns the active job of one printer."""

    def __init__(
        self,
        printer_id: str,
        *,
        method: IntegrationMethod = IntegrationMethod.LEFT,
        max_gap_s: float = 300.0,
    ) -> None:
        self.printer_id = printer_id
        self.method = method
        self.max_gap_s = max_gap_s
        self.active_job: PrintJob | None = None

    # ------------------------------------------------------------------ #
    def restore(self, job: PrintJob | None) -> None:
        """Re-attach a job saved before a restart.

        The next snapshot decides what happens: if the printer is still
        printing the same job the accumulation simply continues (the meter
        delta covers the downtime exactly; with power only, the hole is
        bridged and reported as a gap).
        """
        if job is not None and job.phase is JobPhase.ACTIVE:
            self.active_job = job
            _LOGGER.info("Resuming print '%s' started at %s", job.name, job.started_at)

    # ------------------------------------------------------------------ #
    def process(self, snap: PrinterSnapshot) -> list[Transition]:
        """Feed one snapshot. Returns the transitions it caused."""
        activity = classify_status(snap.status)
        job = self.active_job

        if job is None:
            if activity is PrinterActivity.ACTIVE:
                return [Transition(TransitionKind.STARTED, self._start(snap))]
            return []

        # The interval since the previous sample belongs to the running job,
        # whatever the new state is.
        job.electricity.add_sample(snap.electricity)
        self._update_metadata(job, snap)

        if activity is PrinterActivity.ACTIVE:
            if _is_different_job(job.name, snap.job_name):
                # A new print started without us seeing the end of the
                # previous one (HA was down, or the printer skipped states).
                _LOGGER.info("Job changed from '%s' to '%s' without terminal state", job.name, snap.job_name)
                ended = self._end(job, snap, PrintResult.UNKNOWN)
                started = self._start(snap)
                return [Transition(TransitionKind.ENDED, ended), Transition(TransitionKind.STARTED, started)]
            if snap.job_name and not job.name:
                job.name = snap.job_name
            return [Transition(TransitionKind.UPDATED, job)]

        result = _RESULT_FOR_ACTIVITY.get(activity)
        if result is not None:
            return [Transition(TransitionKind.ENDED, self._end(job, snap, result))]

        # UNKNOWN (offline / unavailable): keep the job open.
        return [Transition(TransitionKind.UPDATED, job)]

    # ------------------------------------------------------------------ #
    def _start(self, snap: PrinterSnapshot) -> PrintJob:
        acc = ElectricityAccumulator(method=self.method, max_gap_s=self.max_gap_s)
        acc.add_sample(snap.electricity)
        job = PrintJob(
            printer_id=self.printer_id,
            started_at=snap.timestamp,
            electricity=acc,
            name=snap.job_name,
        )
        self._update_metadata(job, snap)
        self.active_job = job
        _LOGGER.info("Print started: '%s'", job.name)
        return job

    def _end(self, job: PrintJob, snap: PrinterSnapshot, result: PrintResult) -> PrintJob:
        job.electricity.flush()
        job.finished_at = snap.timestamp
        job.result = result
        job.phase = JobPhase.FINALIZING
        self.active_job = None
        _LOGGER.info("Print ended: '%s' result=%s energy=%.4f kWh", job.name, result, job.energy_kwh)
        return job

    @staticmethod
    def _update_metadata(job: PrintJob, snap: PrinterSnapshot) -> None:
        job.printer_state = snap.status
        if snap.progress is not None:
            job.progress = snap.progress
        if snap.remaining_min is not None:
            job.remaining_min = snap.remaining_min


def _is_different_job(current: str | None, new: str | None) -> bool:
    """Both names known and different -> another print."""
    return bool(current) and bool(new) and current != new
