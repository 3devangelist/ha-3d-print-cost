"""The PrintJob aggregate: one physical print, its energy and its electricity cost.

Pure logic, no Home Assistant import.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from ..materials import SOURCE_SLICER, normalize_material, total_cost
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
    # Who decided the result: None = the printer, "user" = marked by hand, "watchdog" = closed by the integration
    result_by: str | None = None
    remarks: list[str] = field(default_factory=list)  # e.g. "interrupted: the printer lost power"

    progress: float | None = None
    remaining_min: float | None = None

    # Filament really used (0.14.0). Read from the printer, never estimated.
    # The largest length seen is kept: Moonraker's history says 0 mm when
    # Klipper restarts or disconnects during the print. When the printer's
    # counter starts again from 0 within the same print (Klipper restarted and
    # the same file was started again), what was used before is kept too.
    filament_seen_mm: float | None = None  # largest length of the printer's current counter
    filament_banked_mm: float = 0.0  # used before the printer's counter started again
    filament_g_per_mm: float | None = None  # from the slicer: weight / length of the whole file
    filament_type: str | None = None  # the material (PLA, PETG…), or what the slicer wrote when it is not one
    filament_price_per_kg: Decimal | None = None  # set when the print ends (printer option or generic price)
    # 0.15.0: where the material came from (slicer, spool, file_name, user) and the price (printer, generic).
    filament_type_source: str | None = None
    filament_price_source: str | None = None

    # ------------------------------------------------------------------ #
    # Filament
    # ------------------------------------------------------------------ #
    def record_filament(self, used_mm: float | None) -> None:
        """A live reading of the printer's counter (mm since the file started).

        ``None``, negative or non-finite values are ignored. A lower value than
        the largest seen means the counter started again (new attempt of the
        same file): the length used so far is kept and the new one adds to it.
        """
        value = _length(used_mm)
        if value is None:
            return
        seen = self.filament_seen_mm
        if seen is not None and value < seen:
            if value > 0:  # a reset counter reads 0 first: only bank once it really moves again
                self.filament_banked_mm += seen
                self.filament_seen_mm = value
            return
        self.filament_seen_mm = value

    def record_final_filament(self, used_mm: float | None) -> None:
        """A final value (Moonraker's history): only ever raises the current counter, never resets it."""
        value = _length(used_mm)
        if value is not None and (self.filament_seen_mm is None or value > self.filament_seen_mm):
            self.filament_seen_mm = value

    @property
    def filament_mm(self) -> float | None:
        """Filament length used by this print (mm). ``None`` when the printer never reported it."""
        if self.filament_seen_mm is None:
            return self.filament_banked_mm or None
        return self.filament_banked_mm + self.filament_seen_mm

    def set_filament_file(self, g_per_mm: float | None, filament_type: str | None) -> None:
        """What the slicer wrote for the file: grams per mm and filament type (unknown values are skipped)."""
        if g_per_mm is not None and math.isfinite(g_per_mm) and g_per_mm > 0:
            self.filament_g_per_mm = g_per_mm
        if filament_type and str(filament_type).strip():
            # "Generic PETG" / "ASA;ASA" → PETG / ASA; a name that is no known material is kept as written.
            self.filament_type = normalize_material(filament_type) or str(filament_type).strip()
            self.filament_type_source = SOURCE_SLICER

    def set_material_seen(self, material: str | None, source: str) -> None:
        """A material seen elsewhere (loaded spool, file name): only when the slicer did not say."""
        if material and self.filament_type is None:
            self.filament_type = material
            self.filament_type_source = source

    @property
    def filament_g(self) -> float | None:
        """Grams used: real length x the slicer's grams per mm. ``None`` when either is unknown."""
        if self.filament_mm is None or self.filament_g_per_mm is None:
            return None
        return self.filament_mm * self.filament_g_per_mm

    @property
    def filament_cost(self) -> Decimal | None:
        """Cost of the filament used, at the printer's price per kg. ``None`` when unknown."""
        grams = self.filament_g
        if grams is None or self.filament_price_per_kg is None:
            return None
        return Decimal(repr(grams)) * self.filament_price_per_kg / Decimal(1000)

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
            "result_by": self.result_by,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_s": round(self.duration_s(now)),
            "energy_kwh": round(self.energy_kwh, 4),
            "electricity_cost": _round(self.electricity_cost, 4),
            "electricity_cost_complete": self.electricity_cost_complete,
            "filament_mm": _round_float(self.filament_mm, 1),
            "filament_m": _round_float(self.filament_mm / 1000, 2) if self.filament_mm is not None else None,
            "filament_g": _round_float(self.filament_g, 2),
            "filament_type": self.filament_type,
            "filament_cost": _round(self.filament_cost, 4),
            "filament_type_source": self.filament_type_source,
            "filament_price_per_kg": _round(self.filament_price_per_kg, 4),
            "filament_price_source": self.filament_price_source,
            "print_cost": total_cost(self.electricity_cost, self.filament_cost),
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
        notes.extend(self.remarks)
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
            "result_by": self.result_by,
            "remarks": list(self.remarks),
            "progress": self.progress,
            "remaining_min": self.remaining_min,
            "filament_seen_mm": self.filament_seen_mm,
            "filament_banked_mm": self.filament_banked_mm,
            "filament_g_per_mm": self.filament_g_per_mm,
            "filament_type": self.filament_type,
            "filament_price_per_kg": _text(self.filament_price_per_kg),
            "filament_type_source": self.filament_type_source,
            "filament_price_source": self.filament_price_source,
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
            result_by=data.get("result_by"),
            remarks=[str(r) for r in data.get("remarks") or []],
            progress=data.get("progress"),
            remaining_min=data.get("remaining_min"),
            filament_seen_mm=_float_or_none(data.get("filament_seen_mm")),
            filament_banked_mm=_float_or_none(data.get("filament_banked_mm")) or 0.0,
            filament_g_per_mm=_float_or_none(data.get("filament_g_per_mm")),
            filament_type=data.get("filament_type") or None,
            filament_price_per_kg=_decimal_or_none(data.get("filament_price_per_kg")),
            filament_type_source=data.get("filament_type_source") or None,
            filament_price_source=data.get("filament_price_source") or None,
            electricity=ElectricityAccumulator.from_dict(data.get("electricity") or {}),
        )


def filament_price(value: Any) -> Decimal | None:
    """The printer's filament price per kg from its options: ``None`` when not set or not a valid price."""
    return _decimal_or_none(value)


def _length(value: Any) -> float | None:
    """A filament length (mm) from the printer: ``None`` unless a finite number >= 0."""
    number = _float_or_none(value)
    return number if number is not None and number >= 0 else None


def _float_or_none(value: Any) -> float | None:
    try:
        number = None if value is None else float(value)
    except (TypeError, ValueError):
        return None
    return number if number is not None and math.isfinite(number) else None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except ArithmeticError:
        return None
    return number if number.is_finite() and number >= 0 else None


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _round_float(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _round(value: Decimal | None, digits: int) -> float | None:
    """Round a Decimal for display / JSON (internal precision is kept)."""
    if value is None:
        return None
    return float(round(value, digits))
