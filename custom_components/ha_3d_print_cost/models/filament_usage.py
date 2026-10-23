"""Filament usage and pricing.

Pure logic, no Home Assistant import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from ..units import to_decimal

ZERO = Decimal(0)


class UsageSource(StrEnum):
    """Where a filament weight comes from, by decreasing trust."""

    HASPELSYNC = "haspelsync"  # real consumption from the G-code (preferred)
    BAMBU_SLOT = "bambu_slot"  # sliced per-slot weight from ha-bambulab
    BAMBU_TOTAL = "bambu_total"  # sliced total weight from ha-bambulab
    ESTIMATED_PROGRESS = "estimated_progress"  # sliced weight x progress (opt-in)
    GCODE_FILE = "gcode_file"  # slicer estimate of a file not printed yet (estimate_cost)


class PriceSource(StrEnum):
    """Which Spoolman field the price per gram was derived from."""

    SPOOL = "spool_price"  # spool.price / spool.initial_weight
    FILAMENT = "filament_price"  # filament.price / filament.weight
    EXTRA_PER_KG = "extra_price_per_kg"  # custom field, already per kg
    EXTRA_PER_G = "extra_price_per_g"  # custom field, already per gram


@dataclass(slots=True)
class SpoolInfo:
    """What we read from Spoolman for one spool (read-only)."""

    spool_id: int
    filament_id: int | None = None
    vendor: str | None = None
    name: str | None = None
    material: str | None = None
    color_hex: str | None = None
    spool_price: Decimal | None = None
    filament_price: Decimal | None = None
    initial_weight_g: float | None = None
    filament_weight_g: float | None = None
    remaining_weight_g: float | None = None
    price_per_kg_extra: Decimal | None = None
    price_per_g_extra: Decimal | None = None
    archived: bool = False

    @property
    def label(self) -> str:
        """Readable name: 'Vendor Name (Material)'."""
        parts = [p for p in (self.vendor, self.name) if p]
        text = " ".join(parts) if parts else f"Spool {self.spool_id}"
        if self.material and self.material not in text:
            text = f"{self.material} {text}"
        return text


@dataclass(slots=True, frozen=True)
class UnitPrice:
    """A normalised price per gram and how it was obtained."""

    per_gram: Decimal
    source: PriceSource


def price_per_gram(spool: SpoolInfo) -> UnitPrice | None:
    """Normalise the price of a spool to <currency>/g.

    Order of precedence:
    1. an explicit per-gram / per-kg price (Spoolman extra field);
    2. the price paid for this spool divided by its initial net weight
       (falls back to the filament's nominal net weight);
    3. the filament's catalogue price divided by its nominal net weight.

    Returns ``None`` when no price can be derived: the caller must expose the
    cost as unknown rather than zero.
    """
    if spool.price_per_g_extra is not None and spool.price_per_g_extra >= 0:
        return UnitPrice(spool.price_per_g_extra, PriceSource.EXTRA_PER_G)
    if spool.price_per_kg_extra is not None and spool.price_per_kg_extra >= 0:
        return UnitPrice(spool.price_per_kg_extra / Decimal(1000), PriceSource.EXTRA_PER_KG)

    if spool.spool_price is not None and spool.spool_price >= 0:
        weight = spool.initial_weight_g or spool.filament_weight_g
        if weight and weight > 0:
            return UnitPrice(spool.spool_price / Decimal(repr(weight)), PriceSource.SPOOL)

    if spool.filament_price is not None and spool.filament_price >= 0:
        weight = spool.filament_weight_g or spool.initial_weight_g
        if weight and weight > 0:
            return UnitPrice(spool.filament_price / Decimal(repr(weight)), PriceSource.FILAMENT)

    return None


@dataclass(slots=True)
class FilamentUsage:
    """Consumption of one filament (one spool / slot) for a print."""

    weight_g: float
    source: UsageSource
    slot: str | None = None  # Haspelsync slot label: A1, B3, HT-A, External...
    spool_id: int | None = None
    material: str | None = None
    color: str | None = None
    name: str | None = None
    vendor: str | None = None
    price_per_g: Decimal | None = None
    price_source: PriceSource | None = None
    cost: Decimal | None = None
    note: str | None = None

    def apply_price(self, spool: SpoolInfo | None) -> None:
        """Fill metadata and cost from a Spoolman spool."""
        if spool is not None:
            self.material = self.material or spool.material
            self.color = self.color or spool.color_hex
            self.name = self.name or spool.name
            self.vendor = self.vendor or spool.vendor
            unit = price_per_gram(spool)
            if unit is not None:
                self.price_per_g = unit.per_gram
                self.price_source = unit.source
        if self.price_per_g is not None:
            self.cost = Decimal(repr(self.weight_g)) * self.price_per_g
        else:
            self.cost = None

    # ------------------------------------------------------------------ #
    def as_dict(self) -> dict[str, Any]:
        """Serialise to JSON-compatible data."""
        return {
            "weight_g": self.weight_g,
            "source": str(self.source),
            "slot": self.slot,
            "spool_id": self.spool_id,
            "material": self.material,
            "color": self.color,
            "name": self.name,
            "vendor": self.vendor,
            "price_per_g": str(self.price_per_g) if self.price_per_g is not None else None,
            "price_source": str(self.price_source) if self.price_source else None,
            "cost": str(self.cost) if self.cost is not None else None,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FilamentUsage:
        """Restore from :meth:`as_dict` output."""
        price_source = data.get("price_source")
        return cls(
            weight_g=float(data.get("weight_g", 0.0)),
            source=UsageSource(data.get("source", UsageSource.HASPELSYNC)),
            slot=data.get("slot"),
            spool_id=data.get("spool_id"),
            material=data.get("material"),
            color=data.get("color"),
            name=data.get("name"),
            vendor=data.get("vendor"),
            price_per_g=to_decimal(data.get("price_per_g")),
            price_source=PriceSource(price_source) if price_source else None,
            cost=to_decimal(data.get("cost")),
            note=data.get("note"),
        )


@dataclass(slots=True)
class FilamentTotals:
    """Aggregated filament weight and cost of a print."""

    weight_g: float | None
    cost: Decimal | None
    cost_complete: bool
    missing: list[str] = field(default_factory=list)


def summarize_usages(usages: list[FilamentUsage]) -> FilamentTotals:
    """Sum weights and costs.

    The total cost is ``None`` as soon as one non-zero usage has no price:
    a partial sum would silently under-report the print.
    """
    if not usages:
        return FilamentTotals(weight_g=None, cost=None, cost_complete=False, missing=["no usage"])

    weight = sum(u.weight_g for u in usages)
    missing: list[str] = []
    cost = ZERO
    for usage in usages:
        if usage.weight_g <= 0:
            continue
        if usage.cost is None:
            missing.append(usage.slot or (f"spool {usage.spool_id}" if usage.spool_id else "unknown"))
        else:
            cost += usage.cost

    if missing:
        return FilamentTotals(weight_g=weight, cost=None, cost_complete=False, missing=missing)
    return FilamentTotals(weight_g=weight, cost=cost, cost_complete=True)
