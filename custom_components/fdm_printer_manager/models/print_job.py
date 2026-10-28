"""The PrintJob aggregate: one physical print, its energy and its electricity cost.

Pure logic, no Home Assistant import.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from .cost import ElectricityAccumulator

ZERO = Decimal(0)


class JobPhase(StrEnum):
    """Where the job is in its life cycle."""

    ACTIVE = "active"  # printer preparing / printing / paused
    FINALIZING = "finalizing"  # electricity frozen, being stored and announced
    COMPLETED = "completed"  # everything known, kept as the last print


class PrintResult(StrEnum):
    """Outcome of a print."""

    SUCCESS = "success"
    CANCELLED = "cancelled"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class PrintJob:
    """One print, from first heating to final cost."""

    printer_id: str
    started_at: datetime
    electricity: ElectricityAccumulator
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str | None = None
    finished_at: datetime | None = None
    phase: JobPhase = JobPhase.ACTIVE
    result: PrintResult | None = None
    printer_state: str | None = None  # raw last state reported by the printer

    progress: float | None = None
    remaining_min: float | None = None

    # ------------------------------------------------------------------ #
    # Derived values
    # ------------------------------------------------------------------ #
    def duration_s(self, now: datetime | None = None) -> float:
        """Elapsed (or total) duration in seconds."""
        end = self.finished_at or now or self.electricity.last_timestamp or self.started_at
        return max((end - self.started_at).total_seconds(), 0.0)

    @property
    def energy_kwh(self) -> float:
        """Energy consumed so far (power estimate until the meter confirms it)."""
        return self.electricity.total_energy_kwh

    @property
    def energy_start(self) -> float | None:
        """Meter value at the start (kWh), when a meter is configured."""
        return self.electricity.energy_start_kwh

    @property
    def energy_end(self) -> float | None:
        """Meter value at the last sample (kWh)."""
        return self.electricity.energy_end_kwh

    @property
    def electricity_cost(self) -> Decimal | None:
        """Electricity cost.

        ``None`` when no kWh at all could be priced. When only part of the
        energy was priced the partial value is returned and
        ``electricity_cost_complete`` is ``False``.
        """
        acc = self.electricity
        if acc.total_cost == ZERO and acc.total_uncosted_energy_kwh > 0:
            return None
        return acc.total_cost

    @property
    def electricity_cost_complete(self) -> bool:
        """Whether every kWh had a known price."""
        return self.electricity.cost_complete

    def cost_per_hour(self, now: datetime | None = None) -> Decimal | None:
        """Cost of the print per hour of printing."""
        total = self.electricity_cost
        hours = self.duration_s(now) / 3600.0
        if total is None or hours < 1 / 60:  # avoid silly numbers in the first minute
            return None
        return total / Decimal(repr(hours))

    # ------------------------------------------------------------------ #
    # Views
    # ------------------------------------------------------------------ #
    def summary(self, now: datetime | None = None) -> dict[str, Any]:
        """Flat, rounded view used for events and attributes."""
        return {
            "job_id": self.job_id,
            "name": self.name,
            "result": str(self.result) if self.result else None,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_s": round(self.duration_s(now)),
            "energy_kwh": round(self.energy_kwh, 4),
            "electricity_cost": _round(self.electricity_cost, 4),
            "electricity_cost_complete": self.electricity_cost_complete,
            "notes": self.notes(),
        }

    def notes(self) -> list[str]:
        """Every approximation or missing piece, for transparency."""
        notes = self.electricity.notes
        if not self.electricity.cost_complete:
            notes.append(
                f"{self.electricity.total_uncosted_energy_kwh:.4f} kWh consumed while the electricity price was unknown"
            )
        if self.electricity.gap_seconds:
            notes.append(f"{round(self.electricity.gap_seconds)} s of the print had no fresh sample")
        return notes

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def as_dict(self) -> dict[str, Any]:
        """Full serialisation (active / finalizing jobs)."""
        return {
            "job_id": self.job_id,
            "printer_id": self.printer_id,
            "name": self.name,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "phase": str(self.phase),
            "result": str(self.result) if self.result else None,
            "printer_state": self.printer_state,
            "progress": self.progress,
            "remaining_min": self.remaining_min,
            "electricity": self.electricity.as_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PrintJob:
        """Restore from :meth:`as_dict` output."""
        finished = data.get("finished_at")
        result = data.get("result")
        return cls(
            job_id=data["job_id"],
            printer_id=data["printer_id"],
            name=data.get("name"),
            started_at=datetime.fromisoformat(data["started_at"]),
            finished_at=datetime.fromisoformat(finished) if finished else None,
            phase=JobPhase(data.get("phase", JobPhase.ACTIVE)),
            result=PrintResult(result) if result else None,
            printer_state=data.get("printer_state"),
            progress=data.get("progress"),
            remaining_min=data.get("remaining_min"),
            electricity=ElectricityAccumulator.from_dict(data.get("electricity") or {}),
        )


def _round(value: Decimal | None, digits: int) -> float | None:
    """Round a Decimal for display / JSON (internal precision is kept)."""
    if value is None:
        return None
    return float(round(value, digits))
