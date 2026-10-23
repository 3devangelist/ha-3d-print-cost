"""Aggregates over the stored print history (list of ``PrintJob.summary()``).

Pure logic, no Home Assistant import.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .units import to_decimal, to_float

# Prints shorter than this are ignored for the average power: the heat-up
# phase dominates and would over-estimate long prints.
MIN_DURATION_FOR_POWER_S = 600


def history_totals(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum the costs of past prints.

    A job whose total is unknown (e.g. filament price missing) is counted in
    ``incomplete_jobs`` and only its known parts are added: the totals are
    then a lower bound, which the caller must make visible.
    """
    total = electricity = filament = Decimal(0)
    energy = weight = 0.0
    duration = 0
    incomplete = 0
    results: dict[str, int] = {}
    for job in jobs:
        result = job.get("result") or "unknown"
        results[result] = results.get(result, 0) + 1
        duration += int(job.get("duration_s") or 0)
        energy += to_float(job.get("energy_kwh")) or 0.0
        weight += to_float(job.get("filament_weight_g")) or 0.0
        elec = to_decimal(job.get("electricity_cost"))
        fil = to_decimal(job.get("filament_cost"))
        electricity += elec or 0
        filament += fil or 0
        job_total = to_decimal(job.get("total_cost"))
        if job_total is None:
            incomplete += 1
            job_total = to_decimal(job.get("partial_total_cost")) or Decimal(0)
        total += job_total
    return {
        "prints": len(jobs),
        "results": results,
        "incomplete_jobs": incomplete,
        "duration_s": duration,
        "energy_kwh": round(energy, 4),
        "filament_weight_g": round(weight, 2),
        "electricity_cost": float(round(electricity, 4)),
        "filament_cost": float(round(filament, 4)),
        "total_cost": float(round(total, 4)),
    }


def average_power_w(jobs: list[dict[str, Any]]) -> tuple[float, int] | None:
    """Mean power of successful past prints, weighted by duration.

    Returns ``(watts, number of prints used)`` or ``None`` without data.
    """
    energy_kwh = 0.0
    hours = 0.0
    used = 0
    for job in jobs:
        if job.get("result") != "success":
            continue
        duration = to_float(job.get("duration_s")) or 0.0
        energy = to_float(job.get("energy_kwh")) or 0.0
        if duration < MIN_DURATION_FOR_POWER_S or energy <= 0:
            continue
        energy_kwh += energy
        hours += duration / 3600
        used += 1
    if not used:
        return None
    return energy_kwh / hours * 1000, used
