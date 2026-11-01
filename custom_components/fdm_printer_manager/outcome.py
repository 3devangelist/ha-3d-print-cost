"""Results of finished prints: marking one by hand, and the numbers of the history.

Pure logic, no Home Assistant import.
"""

from __future__ import annotations

from typing import Any

from .models import PrintResult

# What a user can mark a finished print as.
MARKABLE: tuple[str, ...] = (str(PrintResult.SUCCESS), str(PrintResult.FAILED))


def mark_history(history: list[dict[str, Any]], job_id: str, result: str) -> bool:
    """Set the result of one row of the history (``True`` when the print was found)."""
    found = False
    for row in history:
        if row.get("job_id") == job_id:
            row["result"] = result
            row["result_by"] = "user"
            found = True
    return found


def history_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts and costs per result, the success rate and what failed prints cost.

    The success rate counts only successes and failures: a cancelled print was
    stopped on purpose, an unknown one cannot be judged.
    """
    counts = {str(r): 0 for r in PrintResult}
    costs = {str(r): 0.0 for r in PrintResult}
    energy = {str(r): 0.0 for r in PrintResult}
    for row in rows:
        result = row.get("result") or str(PrintResult.UNKNOWN)
        if result not in counts:
            result = str(PrintResult.UNKNOWN)
        counts[result] += 1
        costs[result] += float(row.get("electricity_cost") or 0)
        energy[result] += float(row.get("energy_kwh") or 0)
    judged = counts["success"] + counts["failed"]
    return {
        "prints": len(rows),
        "counts": counts,
        "cost": {k: round(v, 4) for k, v in costs.items()},
        "energy_kwh": {k: round(v, 4) for k, v in energy.items()},
        "success_rate": round(100 * counts["success"] / judged, 1) if judged else None,
        "wasted_cost": round(costs["failed"] + costs["cancelled"], 4),
    }
