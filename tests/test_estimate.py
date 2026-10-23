"""Cost estimate of a sliced file and history aggregates (pure logic)."""

from decimal import Decimal

import pytest

from custom_components.ha_3d_print_cost.estimate import (
    LoadedSlot,
    SpoolMatch,
    assign_spools,
    estimate_cost,
)
from custom_components.ha_3d_print_cost.gcode import GcodeInfo, SlicedFilament
from custom_components.ha_3d_print_cost.history import average_power_w, history_totals
from custom_components.ha_3d_print_cost.models import SpoolInfo

BLACK = SlicedFilament(index=1, weight_g=62.4, material="PLA", color="#000000")
WHITE = SlicedFilament(index=2, weight_g=22.2, material="PLA", color="#FFFFFF")
INFO = GcodeInfo(duration_s=3 * 3600, filaments=[BLACK, WHITE], plate=1, plates=[1])

# 20 €/kg and 25 €/kg
SPOOLS = {
    23: SpoolInfo(spool_id=23, spool_price=Decimal(20), initial_weight_g=1000, material="PLA"),
    45: SpoolInfo(spool_id=45, spool_price=Decimal(25), initial_weight_g=1000, material="PLA"),
}


# --------------------------------------------------------------------------- #
# Spool assignment
# --------------------------------------------------------------------------- #
def test_spool_ids_are_positional() -> None:
    result = assign_spools([BLACK, WHITE], [23, 45], [])
    assert [(a.spool_id, a.match) for a in result] == [(23, SpoolMatch.PARAMETER), (45, SpoolMatch.PARAMETER)]


def test_ams_match_by_material_and_colour() -> None:
    loaded = [
        LoadedSlot(slot="A1", spool_id=23, material="pla", color="#000000FF"),
        LoadedSlot(slot="A2", spool_id=45, material="PLA", color="#FFFFFF"),
        LoadedSlot(slot="A3", spool_id=99, material="PETG", color="#000000"),
    ]
    result = assign_spools([BLACK, WHITE], None, loaded)
    assert [(a.spool_id, a.slot, a.match) for a in result] == [
        (23, "A1", SpoolMatch.AMS),
        (45, "A2", SpoolMatch.AMS),
    ]


def test_ambiguous_ams_match_stays_unknown() -> None:
    loaded = [
        LoadedSlot(slot="A1", spool_id=23, material="PLA", color="#000000"),
        LoadedSlot(slot="B1", spool_id=24, material="PLA", color="#000000"),
    ]
    # First filament given explicitly, second has two candidates -> none.
    result = assign_spools([WHITE, BLACK], [45], loaded)
    assert [(a.spool_id, a.match) for a in result] == [(45, SpoolMatch.PARAMETER), (None, SpoolMatch.NONE)]


# --------------------------------------------------------------------------- #
# Estimate
# --------------------------------------------------------------------------- #
def _estimate(spools=SPOOLS, *, power=150.0, price=Decimal("0.20"), spool_ids=(23, 45)):
    assignments = assign_spools(INFO.filaments, list(spool_ids), [])
    return estimate_cost(
        INFO, assignments, spools, average_power_w=power, power_source="parameter", price_per_kwh=price
    )


def test_full_estimate() -> None:
    result = _estimate()
    filament = Decimal("62.4") * Decimal("0.02") + Decimal("22.2") * Decimal("0.025")
    electricity = Decimal("0.45") * Decimal("0.20")  # 150 W x 3 h
    assert result.filament_cost == filament
    assert result.energy_kwh == pytest.approx(0.45)
    assert float(result.electricity_cost) == pytest.approx(float(electricity))
    assert float(result.total_cost) == pytest.approx(float(filament + electricity))

    data = result.as_dict("EUR")
    assert data["currency"] == "EUR"
    assert data["estimated_duration_s"] == 10800
    assert data["filament_weight_g"] == pytest.approx(84.6)
    assert [f["price_per_kg"] for f in data["filaments"]] == [20.0, 25.0]
    assert data["total_cost"] == pytest.approx(float(filament + electricity), abs=1e-4)
    assert "electricity priced at the current tariff for the whole print" in data["notes"]


def test_missing_price_never_becomes_zero() -> None:
    spools = {23: SPOOLS[23], 45: SpoolInfo(spool_id=45)}  # no price
    result = _estimate(spools)
    assert result.filament_cost is None
    assert result.total_cost is None
    assert result.partial_total_cost == pytest.approx(Decimal("62.4") * Decimal("0.02") + Decimal("0.09"))
    assert result.usages[1].note == "spool 45 has no price in Spoolman"


def test_unknown_spool_and_unreachable_spoolman() -> None:
    result = _estimate({23: SPOOLS[23], 45: None})
    assert result.usages[1].note == "spool 45 not found in Spoolman"
    result = _estimate({})
    assert result.usages[0].note == "Spoolman not configured or unreachable"
    assert result.filament_cost is None


def test_no_power_or_price() -> None:
    result = _estimate(power=None)
    assert result.energy_kwh is None
    assert result.electricity_cost is None
    assert result.total_cost is None
    assert result.power_source is None
    result = _estimate(price=None)
    assert result.energy_kwh == pytest.approx(0.45)
    assert result.electricity_cost is None


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #
HISTORY = [
    {
        "result": "success",
        "duration_s": 7200,
        "energy_kwh": 0.3,
        "filament_weight_g": 50.0,
        "electricity_cost": 0.05,
        "filament_cost": 1.0,
        "total_cost": 1.05,
        "partial_total_cost": 1.05,
    },
    {
        "result": "cancelled",
        "duration_s": 1800,
        "energy_kwh": 0.2,
        "filament_weight_g": 10.0,
        "electricity_cost": 0.03,
        "filament_cost": None,
        "total_cost": None,
        "partial_total_cost": 0.03,
    },
    {
        "result": "success",
        "duration_s": 3600,
        "energy_kwh": 0.15,
        "filament_weight_g": None,
        "electricity_cost": 0.02,
        "filament_cost": 0.5,
        "total_cost": 0.52,
        "partial_total_cost": 0.52,
    },
    {"result": "success", "duration_s": 120, "energy_kwh": 0.05, "total_cost": 0.01},  # too short
]


def test_history_totals() -> None:
    totals = history_totals(HISTORY)
    assert totals["prints"] == 4
    assert totals["results"] == {"success": 3, "cancelled": 1}
    assert totals["incomplete_jobs"] == 1
    assert totals["total_cost"] == pytest.approx(1.05 + 0.03 + 0.52 + 0.01)
    assert totals["filament_cost"] == pytest.approx(1.5)
    assert totals["energy_kwh"] == pytest.approx(0.7)
    assert totals["filament_weight_g"] == pytest.approx(60.0)
    assert totals["duration_s"] == 12720


def test_history_totals_empty() -> None:
    assert history_totals([])["total_cost"] == 0.0


def test_average_power_uses_long_successful_prints() -> None:
    watts, count = average_power_w(HISTORY)
    assert count == 2
    assert watts == pytest.approx((0.3 + 0.15) / 3 * 1000)  # 150 W
    assert average_power_w([HISTORY[1]]) is None
