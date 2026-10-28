"""Resolve the filament weight and cost of a finished print.

Order of trust (see project instructions §16):

1. HaspelSync: real consumption computed from the G-code, cancelled prints
   pro-rated per layer. Used as-is, never recomputed.
2. ha-bambulab per-slot sliced weights, mapped to spools with the slot
   snapshot taken from HaspelSync when the print started.
3. ha-bambulab total sliced weight (cost only if a single spool is involved).
4. Nothing: weight / cost stay unknown rather than wrong.

Fallbacks 2 and 3 are *planned* weights. They are only used for successful
prints, or — when the user explicitly enabled it — multiplied by the progress
for failed / cancelled prints.

No Home Assistant import: adapters are injected and mocked in tests.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

from .adapters.spoolman import SpoolNotFoundError
from .const import FilamentFallback
from .models import (
    FilamentStatus,
    FilamentUsage,
    PrintJob,
    PrintResult,
    SpoolInfo,
    UsageSource,
    summarize_usages,
)

_LOGGER = logging.getLogger(__name__)


class UsageProvider(Protocol):
    """What we need from HaspelSync."""

    async def get_print_usage(self, printer_id, *, job_name, started_at): ...  # noqa: D102


class SpoolProvider(Protocol):
    """What we need from Spoolman."""

    async def get_spool(self, spool_id: int) -> SpoolInfo: ...  # noqa: D102


@dataclass(slots=True)
class ResolveOutcome:
    """Result of one resolution attempt."""

    done: bool  # False -> try again later
    status: FilamentStatus = FilamentStatus.UNAVAILABLE
    usages: list[FilamentUsage] = field(default_factory=list)
    result_override: PrintResult | None = None
    note: str | None = None


# --------------------------------------------------------------------------- #
# Slot naming
# --------------------------------------------------------------------------- #
_AMS_TRAY = re.compile(r"^AMS\s+(\d+)\s+Tray\s+(\d+)$", re.IGNORECASE)
_AMS_HT = re.compile(r"^AMS\s+HT\s+(\d+)$", re.IGNORECASE)
_EXTERNAL = re.compile(r"^External\s+Spool(?:\s+(\d+))?$", re.IGNORECASE)


def bambu_slot_to_label(name: str) -> str | None:
    """'AMS 2 Tray 3' -> 'B3', 'AMS HT 1' -> 'HT-A', 'External Spool 2' -> 'External-2'.

    ha-bambulab names slots in English in the ``print_weight`` attributes;
    HaspelSync labels them with a unit letter. No fixed number of AMS units
    is assumed.
    """
    name = name.strip()
    if m := _AMS_TRAY.match(name):
        unit, tray = int(m.group(1)), int(m.group(2))
        return f"{_letter(unit)}{tray}" if unit >= 1 else None
    if m := _AMS_HT.match(name):
        unit = int(m.group(1))
        return f"HT-{_letter(unit)}" if unit >= 1 else None
    if m := _EXTERNAL.match(name):
        idx = m.group(1)
        return "External" if not idx or idx == "1" else f"External-{idx}"
    return None


def _letter(unit: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA (no fixed AMS count)."""
    out = ""
    while unit > 0:
        unit, rem = divmod(unit - 1, 26)
        out = chr(65 + rem) + out
    return out


# --------------------------------------------------------------------------- #
# Resolver
# --------------------------------------------------------------------------- #
class FilamentResolver:
    """Combine HaspelSync, ha-bambulab and Spoolman into priced usages."""

    def __init__(
        self,
        *,
        haspelsync: UsageProvider | None,
        haspelsync_printer_id: str | None,
        spoolman: SpoolProvider | None,
        fallback: FilamentFallback,
    ) -> None:
        self._haspelsync = haspelsync
        self._hs_printer_id = haspelsync_printer_id
        self._spoolman = spoolman
        self._fallback = fallback

    @property
    def enabled(self) -> bool:
        """Filament tracking is on when at least one source is configured."""
        return self._haspelsync is not None or self._spoolman is not None

    async def resolve(self, job: PrintJob, *, last_attempt: bool) -> ResolveOutcome:
        """One attempt. ``last_attempt`` lets the fallback kick in."""
        usages: list[FilamentUsage] | None = None
        result_override: PrintResult | None = None
        notes: list[str] = []

        # 1. HaspelSync -------------------------------------------------
        if self._haspelsync is not None and self._hs_printer_id:
            try:
                booked = await self._haspelsync.get_print_usage(
                    self._hs_printer_id, job_name=job.name, started_at=job.started_at
                )
            except Exception as err:  # noqa: BLE001 - adapter errors are all recoverable
                _LOGGER.warning("HaspelSync unavailable while resolving filament: %s", err)
                booked = None
                notes.append("HaspelSync unavailable")
            if booked is not None:
                result_override = booked.result
                if booked.usages:
                    usages = booked.usages
                    if booked.unbooked:
                        notes.append("HaspelSync could not assign a spool to: " + ", ".join(booked.unbooked))
                elif booked.note:
                    notes.append(f"HaspelSync: {booked.note}")
            if booked is None and not last_attempt:
                return ResolveOutcome(done=False, status=FilamentStatus.PENDING)

        # 2./3. ha-bambulab fallback -------------------------------------
        if usages is None:
            usages = self._fallback_usages(job, result_override or job.result, notes)

        if not usages:
            notes.append("no filament consumption available")
            return ResolveOutcome(
                done=True,
                status=FilamentStatus.UNAVAILABLE,
                result_override=result_override,
                note="; ".join(notes),
            )

        # 4. Prices from Spoolman ----------------------------------------
        await self._apply_prices(usages, notes)
        totals = summarize_usages(usages)
        status = FilamentStatus.RESOLVED if totals.cost_complete else FilamentStatus.PARTIAL
        if totals.missing:
            notes.append("no price for: " + ", ".join(totals.missing))
        return ResolveOutcome(
            done=True,
            status=status,
            usages=usages,
            result_override=result_override,
            note="; ".join(notes) or None,
        )

    # ------------------------------------------------------------------ #
    def _fallback_usages(self, job: PrintJob, result: PrintResult | None, notes: list[str]) -> list[FilamentUsage]:
        if self._fallback is FilamentFallback.NONE:
            return []
        if result is PrintResult.SUCCESS:
            factor = 1.0
            source_note = "planned (sliced) weight used"
        elif self._fallback is FilamentFallback.PLANNED_PROGRESS and job.progress is not None:
            factor = max(min(job.progress / 100.0, 1.0), 0.0)
            source_note = f"estimated as planned weight x {job.progress:.0f}% progress"
        else:
            notes.append("print not successful: planned weight not used")
            return []

        usages: list[FilamentUsage] = []
        source = UsageSource.BAMBU_SLOT if factor == 1.0 else UsageSource.ESTIMATED_PROGRESS
        for slot_name, grams in job.planned_slot_weights.items():
            label = bambu_slot_to_label(slot_name) or slot_name
            if not grams or grams <= 0:
                continue
            usages.append(
                FilamentUsage(
                    weight_g=float(grams) * factor,
                    source=source,
                    slot=label,
                    spool_id=job.slot_spools.get(label),
                )
            )

        if not usages and job.planned_weight_g:
            # Only the total is known: attributable to a spool only if a
            # single spool was loaded for this print.
            spool_ids = set(job.slot_spools.values())
            single = next(iter(spool_ids)) if len(spool_ids) == 1 else None
            usages.append(
                FilamentUsage(
                    weight_g=float(job.planned_weight_g) * factor,
                    source=UsageSource.BAMBU_TOTAL if factor == 1.0 else UsageSource.ESTIMATED_PROGRESS,
                    spool_id=single,
                )
            )

        if usages:
            notes.append(source_note)
        return usages

    async def _apply_prices(self, usages: list[FilamentUsage], notes: list[str]) -> None:
        cache: dict[int, SpoolInfo | None] = {}
        spoolman_down = False
        for usage in usages:
            spool: SpoolInfo | None = None
            if usage.spool_id is not None and self._spoolman is not None and not spoolman_down:
                if usage.spool_id not in cache:
                    try:
                        cache[usage.spool_id] = await self._spoolman.get_spool(usage.spool_id)
                    except SpoolNotFoundError:
                        _LOGGER.warning("Spool %s does not exist in Spoolman", usage.spool_id)
                        cache[usage.spool_id] = None
                    except Exception as err:  # noqa: BLE001 - any adapter failure
                        _LOGGER.warning("Spoolman unavailable for spool %s: %s", usage.spool_id, err)
                        cache[usage.spool_id] = None
                        spoolman_down = True
                        notes.append("Spoolman unavailable")
                spool = cache.get(usage.spool_id)
            usage.apply_price(spool)


def apply_outcome(job: PrintJob, outcome: ResolveOutcome) -> None:
    """Copy a final outcome into the job."""
    job.filament_status = outcome.status
    job.filament_usages = outcome.usages
    job.filament_note = outcome.note
    # HaspelSync knows the difference between FAILED and CANCEL; the printer
    # sensor does not always.
    if outcome.result_override is not None and job.result in (
        PrintResult.FAILED,
        PrintResult.UNKNOWN,
        None,
    ):
        job.result = outcome.result_override
