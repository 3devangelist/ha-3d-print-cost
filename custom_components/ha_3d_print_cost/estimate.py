"""Cost estimate of a sliced file, before printing it.

Inputs are the slicer estimates (:mod:`.gcode`), the spools that will be
used (given by the user or matched against the AMS), the mean power of past
prints and the current electricity price.

The electricity part uses the price *now*: the future tariff is unknown, so
the estimate says so in its notes. Real prints are still costed interval by
interval by the tracker.

Pure logic, no Home Assistant import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .gcode import GcodeInfo, SlicedFilament
from .models import FilamentUsage, SpoolInfo, UsageSource, summarize_usages


class SpoolMatch:
    """How a sliced filament was associated with a spool."""

    PARAMETER = "parameter"  # spool_ids given in the service call
    AMS = "ams"  # same material and colour as a loaded AMS slot
    NONE = "none"


@dataclass(slots=True, frozen=True)
class LoadedSlot:
    """A spool currently loaded in the printer (from HaspelSync)."""

    slot: str
    spool_id: int
    material: str | None = None
    color: str | None = None


@dataclass(slots=True)
class SpoolAssignment:
    """Spool chosen for one sliced filament."""

    spool_id: int | None
    match: str
    slot: str | None = None


def _same_material(a: str | None, b: str | None) -> bool:
    return bool(a and b and a.strip().upper() == b.strip().upper())


def _same_color(a: str | None, b: str | None) -> bool:
    return bool(a and b and a.strip().lstrip("#").upper()[:6] == b.strip().lstrip("#").upper()[:6])


def assign_spools(
    filaments: list[SlicedFilament],
    spool_ids: list[int] | None,
    loaded: list[LoadedSlot],
) -> list[SpoolAssignment]:
    """Pick a spool for every sliced filament.

    ``spool_ids`` is positional (1st id -> 1st filament of the plate). Without
    one, a loaded AMS slot is used only when exactly one has the same material
    and colour: guessing between two identical spools could price the wrong
    one, so the cost stays unknown instead.
    """
    ids = list(spool_ids or [])
    assignments: list[SpoolAssignment] = []
    for position, filament in enumerate(filaments):
        if position < len(ids):
            assignments.append(SpoolAssignment(spool_id=ids[position], match=SpoolMatch.PARAMETER))
            continue
        candidates = [
            s for s in loaded if _same_material(s.material, filament.material) and _same_color(s.color, filament.color)
        ]
        if len(candidates) == 1:
            slot = candidates[0]
            assignments.append(SpoolAssignment(spool_id=slot.spool_id, match=SpoolMatch.AMS, slot=slot.slot))
        else:
            assignments.append(SpoolAssignment(spool_id=None, match=SpoolMatch.NONE))
    return assignments


@dataclass(slots=True)
class CostEstimate:
    """Result of :func:`estimate_cost`."""

    info: GcodeInfo
    usages: list[FilamentUsage]
    assignments: list[SpoolAssignment]
    filament_cost: Decimal | None
    average_power_w: float | None
    power_source: str | None
    energy_kwh: float | None
    price_per_kwh: Decimal | None
    electricity_cost: Decimal | None
    notes: list[str] = field(default_factory=list)

    @property
    def total_cost(self) -> Decimal | None:
        """Electricity + filament, unknown if one of them is."""
        if self.filament_cost is None or self.electricity_cost is None:
            return None
        return self.filament_cost + self.electricity_cost

    @property
    def partial_total_cost(self) -> Decimal:
        """Sum of the known parts (explicitly partial)."""
        known_filament = sum((u.cost for u in self.usages if u.cost is not None), Decimal(0))
        return known_filament + (self.electricity_cost or Decimal(0))

    def as_dict(self, currency: str) -> dict[str, Any]:
        """JSON-serialisable response, rounded for display."""

        def money(value: Decimal | None) -> float | None:
            return None if value is None else float(round(value, 4))

        return {
            "plate": self.info.plate,
            "plates": self.info.plates,
            "slicer": self.info.slicer,
            "estimated_duration_s": round(self.info.duration_s) if self.info.duration_s is not None else None,
            "filament_weight_g": round(self.info.weight_g, 2) if self.info.weight_g is not None else None,
            "filaments": [
                {
                    "index": filament.index,
                    "material": usage.material,
                    "color": usage.color,
                    "weight_g": round(usage.weight_g, 2),
                    "spool_id": usage.spool_id,
                    "spool_match": assignment.match,
                    "slot": assignment.slot,
                    "name": usage.name,
                    "vendor": usage.vendor,
                    "price_per_kg": money(usage.price_per_g * 1000) if usage.price_per_g is not None else None,
                    "cost": money(usage.cost),
                    "note": usage.note,
                }
                for filament, usage, assignment in zip(self.info.filaments, self.usages, self.assignments, strict=True)
            ],
            "filament_cost": money(self.filament_cost),
            "average_power_w": round(self.average_power_w, 1) if self.average_power_w is not None else None,
            "power_source": self.power_source,
            "energy_kwh": round(self.energy_kwh, 4) if self.energy_kwh is not None else None,
            "price_per_kwh": money(self.price_per_kwh),
            "electricity_cost": money(self.electricity_cost),
            "total_cost": money(self.total_cost),
            "partial_total_cost": money(self.partial_total_cost),
            "currency": currency,
            "notes": self.notes,
        }


def estimate_cost(
    info: GcodeInfo,
    assignments: list[SpoolAssignment],
    spools: dict[int, SpoolInfo | None],  # absent key: Spoolman not queried / unreachable
    *,
    average_power_w: float | None,
    power_source: str | None,
    price_per_kwh: Decimal | None,
) -> CostEstimate:
    """Combine slicer estimates, spool prices and electricity into a cost."""
    notes: list[str] = []

    usages: list[FilamentUsage] = []
    for filament, assignment in zip(info.filaments, assignments, strict=True):
        usage = FilamentUsage(
            weight_g=filament.weight_g,
            source=UsageSource.GCODE_FILE,
            slot=assignment.slot or f"filament {filament.index}",
            spool_id=assignment.spool_id,
            material=filament.material,
            color=filament.color,
        )
        if assignment.spool_id is None:
            usage.note = "no spool: pass spool_ids, or load a single matching spool in the AMS"
        elif assignment.spool_id not in spools:
            usage.note = "Spoolman not configured or unreachable"
        elif spools[assignment.spool_id] is None:
            usage.note = f"spool {assignment.spool_id} not found in Spoolman"
        usage.apply_price(spools.get(assignment.spool_id) if assignment.spool_id is not None else None)
        if assignment.spool_id is not None and usage.cost is None and usage.note is None:
            usage.note = f"spool {assignment.spool_id} has no price in Spoolman"
        usages.append(usage)

    totals = summarize_usages(usages)
    if not info.filaments:
        notes.append("the slicer reported no filament")
    elif totals.cost is None:
        notes.append(f"filament cost unknown for: {', '.join(totals.missing)}")

    energy_kwh = None
    electricity_cost = None
    if info.duration_s is None:
        notes.append("no print time in the file: electricity cannot be estimated")
    elif average_power_w is None:
        notes.append("no average power yet: finish a print or pass power_w")
    else:
        energy_kwh = average_power_w / 1000 * info.duration_s / 3600
        if price_per_kwh is None:
            notes.append("electricity price unavailable: energy estimated without cost")
        else:
            electricity_cost = Decimal(repr(energy_kwh)) * price_per_kwh
            notes.append("electricity priced at the current tariff for the whole print")

    return CostEstimate(
        info=info,
        usages=usages,
        assignments=assignments,
        filament_cost=totals.cost if info.filaments else None,
        average_power_w=average_power_w,
        power_source=power_source if average_power_w is not None else None,
        energy_kwh=energy_kwh,
        price_per_kwh=price_per_kwh,
        electricity_cost=electricity_cost,
        notes=notes,
    )
