"""Electricity accumulation, including a tariff change during the print."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from custom_components.ha_3d_print_cost.const import IntegrationMethod
from custom_components.ha_3d_print_cost.models import ElectricityAccumulator, ElectricitySample

T0 = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
HP = Decimal("0.1876")
HC = Decimal("0.1405")


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def run_constant_power(acc: ElectricityAccumulator, watts: float, step_min: float = 1.0) -> None:
    """14:00 -> 15:00 at constant power, price changes at 14:06."""
    minute = 0.0
    while minute <= 60:
        price = HP if minute < 6 else HC
        acc.add_sample(ElectricitySample(at(minute), power_w=watts, price_per_kwh=price))
        minute += step_min


def test_tariff_change_is_integrated_not_last_price():
    acc = ElectricityAccumulator()
    run_constant_power(acc, 600)  # 0.6 kW for 1 h = 0.6 kWh

    assert acc.energy_kwh == pytest.approx(0.6)
    expected = Decimal("0.6") * (Decimal(6) / 60 * HP + Decimal(54) / 60 * HC)
    assert acc.cost == pytest.approx(expected, abs=Decimal("1e-9"))
    # Must differ from energy x last price, and from energy x first price
    assert acc.cost != pytest.approx(Decimal("0.6") * HC, abs=Decimal("1e-6"))
    assert acc.cost != pytest.approx(Decimal("0.6") * HP, abs=Decimal("1e-6"))
    assert acc.cost_complete
    assert acc.price_min == HC
    assert acc.price_max == HP


def test_price_change_sampled_exactly_at_change():
    """The coordinator samples on every price change: exact split."""
    acc = ElectricityAccumulator()
    acc.add_sample(ElectricitySample(at(0), power_w=1000, price_per_kwh=HP))
    acc.add_sample(ElectricitySample(at(6), power_w=1000, price_per_kwh=HC))  # price change event
    acc.add_sample(ElectricitySample(at(60), power_w=1000, price_per_kwh=HC))
    assert acc.energy_kwh == pytest.approx(1.0)
    assert acc.cost == Decimal("0.1") * HP + Decimal("0.9") * HC


def test_energy_meter_preferred_over_power():
    acc = ElectricityAccumulator()
    # Power sensor says 0 W (wrong / stale) but the meter is the truth.
    acc.add_sample(ElectricitySample(at(0), power_w=0, energy_kwh=100.0, price_per_kwh=HP))
    acc.add_sample(ElectricitySample(at(6), power_w=0, energy_kwh=100.05, price_per_kwh=HC))
    acc.add_sample(ElectricitySample(at(60), power_w=0, energy_kwh=100.5, price_per_kwh=HC))
    assert acc.energy_kwh == pytest.approx(0.5)
    assert acc.energy_start_kwh == 100.0
    assert acc.energy_end_kwh == 100.5
    expected = Decimal(repr(0.05)) * HP + Decimal(repr(100.5 - 100.05)) * HC
    assert acc.cost == pytest.approx(expected, abs=Decimal("1e-9"))


def test_meter_reset_falls_back_to_power():
    acc = ElectricityAccumulator()
    acc.add_sample(ElectricitySample(at(0), power_w=600, energy_kwh=5.0, price_per_kwh=HP))
    acc.add_sample(ElectricitySample(at(30), power_w=600, energy_kwh=0.01, price_per_kwh=HP))  # plug reboot
    assert acc.energy_kwh == pytest.approx(0.3)
    assert acc.meter_resets == 1
    assert acc.notes


def test_unknown_price_is_not_zero():
    acc = ElectricityAccumulator()
    acc.add_sample(ElectricitySample(at(0), power_w=1000, price_per_kwh=None))
    acc.add_sample(ElectricitySample(at(30), power_w=1000, price_per_kwh=HP))
    acc.add_sample(ElectricitySample(at(60), power_w=1000, price_per_kwh=HP))
    assert acc.energy_kwh == pytest.approx(1.0)
    assert acc.uncosted_energy_kwh == pytest.approx(0.5)
    assert not acc.cost_complete
    assert acc.cost == Decimal("0.5") * HP


def test_trapezoidal_method():
    acc = ElectricityAccumulator(method=IntegrationMethod.TRAPEZOIDAL)
    acc.add_sample(ElectricitySample(at(0), power_w=0, price_per_kwh=HP))
    acc.add_sample(ElectricitySample(at(60), power_w=1000, price_per_kwh=HP))
    assert acc.energy_kwh == pytest.approx(0.5)


def test_left_method_uses_previous_value():
    acc = ElectricityAccumulator(method=IntegrationMethod.LEFT)
    acc.add_sample(ElectricitySample(at(0), power_w=0, price_per_kwh=HP))
    acc.add_sample(ElectricitySample(at(60), power_w=1000, price_per_kwh=HP))
    assert acc.energy_kwh == pytest.approx(0.0)


def test_unavailable_power_keeps_last_value():
    acc = ElectricityAccumulator()
    acc.add_sample(ElectricitySample(at(0), power_w=500, price_per_kwh=HP))
    acc.add_sample(ElectricitySample(at(30), power_w=None, price_per_kwh=HP))
    acc.add_sample(ElectricitySample(at(60), power_w=500, price_per_kwh=HP))
    assert acc.energy_kwh == pytest.approx(0.5)


def test_clock_going_backwards_is_ignored():
    acc = ElectricityAccumulator()
    acc.add_sample(ElectricitySample(at(10), power_w=500, price_per_kwh=HP))
    acc.add_sample(ElectricitySample(at(5), power_w=50000, price_per_kwh=HP))
    assert acc.energy_kwh == 0


def test_long_gap_is_reported():
    acc = ElectricityAccumulator(max_gap_s=300)
    acc.add_sample(ElectricitySample(at(0), power_w=500, price_per_kwh=HP))
    acc.add_sample(ElectricitySample(at(20), power_w=500, price_per_kwh=HP))
    assert acc.gap_seconds == 1200


def test_round_trip_serialisation():
    acc = ElectricityAccumulator()
    run_constant_power(acc, 450, step_min=5)
    restored = ElectricityAccumulator.from_dict(acc.as_dict())
    assert restored.cost == acc.cost
    assert restored.energy_kwh == acc.energy_kwh
    assert restored.last_timestamp == acc.last_timestamp
    assert restored.last_price == acc.last_price


def test_meter_only_without_power_sensor():
    acc = ElectricityAccumulator()
    acc.add_sample(ElectricitySample(at(0), energy_kwh=1.0, price_per_kwh=HP))
    acc.add_sample(ElectricitySample(at(6), energy_kwh=1.1, price_per_kwh=HC))
    acc.add_sample(ElectricitySample(at(60), energy_kwh=2.0, price_per_kwh=HC))
    assert acc.energy_kwh == pytest.approx(1.0)
    expected = Decimal(repr(1.1 - 1.0)) * HP + Decimal(repr(2.0 - 1.1)) * HC
    assert acc.cost == pytest.approx(expected, abs=Decimal("1e-9"))


def test_meter_outage_is_not_double_counted():
    acc = ElectricityAccumulator()
    acc.add_sample(ElectricitySample(at(0), power_w=500, energy_kwh=10.0, price_per_kwh=HP))
    acc.add_sample(ElectricitySample(at(20), power_w=500, energy_kwh=None, price_per_kwh=HP))
    acc.add_sample(ElectricitySample(at(40), power_w=500, energy_kwh=None, price_per_kwh=HP))
    acc.add_sample(ElectricitySample(at(60), power_w=500, energy_kwh=10.6, price_per_kwh=HP))
    assert acc.energy_kwh == pytest.approx(0.6)  # the meter wins, estimate dropped
    assert acc.cost == pytest.approx(Decimal("0.6") * HP, abs=Decimal("1e-9"))
    assert acc.pending_kwh == 0


def test_meter_lost_until_the_end_uses_power_estimate():
    acc = ElectricityAccumulator()
    acc.add_sample(ElectricitySample(at(0), power_w=600, energy_kwh=10.0, price_per_kwh=HP))
    acc.add_sample(ElectricitySample(at(30), power_w=600, energy_kwh=None, price_per_kwh=HP))
    acc.add_sample(ElectricitySample(at(60), power_w=600, energy_kwh=None, price_per_kwh=HP))
    assert acc.energy_kwh == 0
    acc.flush()
    assert acc.energy_kwh == pytest.approx(0.6)
    assert acc.notes


def test_stale_meter_shows_power_estimate_until_it_steps():
    # Plug that refreshes its counter rarely: 116 W, meter frozen for 50 min.
    acc = ElectricityAccumulator()
    for minute in range(0, 51, 1):
        acc.add_sample(ElectricitySample(at(minute), power_w=116, energy_kwh=17.57, price_per_kwh=HC))
    assert acc.energy_kwh == 0  # nothing confirmed by the meter
    assert acc.total_energy_kwh == pytest.approx(0.116 * 50 / 60)
    assert acc.total_cost == pytest.approx(Decimal(repr(0.116 * 50 / 60)) * HC, abs=Decimal("1e-9"))

    # The meter finally steps: its exact delta replaces the estimate.
    acc.add_sample(ElectricitySample(at(51), power_w=116, energy_kwh=17.67, price_per_kwh=HC))
    assert acc.energy_kwh == pytest.approx(0.10)
    assert acc.pending_kwh == 0
    assert acc.total_energy_kwh == pytest.approx(0.10)


def test_meter_frozen_for_the_whole_print_uses_power():
    acc = ElectricityAccumulator()
    for minute in range(0, 61, 5):
        acc.add_sample(ElectricitySample(at(minute), power_w=120, energy_kwh=17.57, price_per_kwh=HC))
    acc.flush()
    assert acc.energy_kwh == pytest.approx(0.12)
    assert acc.cost == pytest.approx(Decimal(repr(0.12)) * HC, abs=Decimal("1e-9"))
    assert "energy meter did not report the last part of the print, power estimate used" in acc.notes


def test_meter_hole_note_only_once_the_meter_is_back():
    acc = ElectricityAccumulator()
    acc.add_sample(ElectricitySample(at(0), power_w=120, energy_kwh=10.0, price_per_kwh=HC))
    acc.add_sample(ElectricitySample(at(1), power_w=120, energy_kwh=None, price_per_kwh=HC))  # HA restart
    assert acc.notes == []
    acc.add_sample(ElectricitySample(at(30), power_w=120, energy_kwh=10.06, price_per_kwh=HC))
    assert acc.notes == ["energy meter was briefly unavailable, its next reading covered the gap"]
    assert acc.energy_kwh == pytest.approx(0.06)


def test_seconds_without_price_are_not_flagged():
    acc = ElectricityAccumulator()
    acc.add_sample(ElectricitySample(at(0), power_w=120, price_per_kwh=None))  # price sensor restarting
    acc.add_sample(ElectricitySample(at(0.1), power_w=120, price_per_kwh=HC))  # 6 s -> 0.2 Wh
    acc.add_sample(ElectricitySample(at(60), power_w=120, price_per_kwh=HC))
    assert acc.uncosted_energy_kwh == pytest.approx(0.0002)
    assert acc.cost_complete
