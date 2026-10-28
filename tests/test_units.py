"""Unit conversions."""

from decimal import Decimal

import pytest

from custom_components.fdm_printer_manager.units import (
    UnsupportedUnitError,
    energy_to_kwh,
    is_supported_price_unit,
    power_to_w,
    price_to_per_kwh,
    to_decimal,
    to_float,
    w_to_kw,
)


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [(1500, "Wh", 1.5), (2, "kWh", 2), (0.5, "MWh", 500), (250, "wh", 0.25)],
)
def test_energy_to_kwh(value, unit, expected):
    assert energy_to_kwh(value, unit) == pytest.approx(expected)


def test_power_conversions():
    assert power_to_w(1.2, "kW") == pytest.approx(1200)
    assert power_to_w(150, "W") == 150
    assert w_to_kw(350) == pytest.approx(0.35)


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("EUR/kWh", Decimal("0.1876")),
        ("€/kWh", Decimal("0.1876")),
        ("c€/kWh", Decimal("0.001876")),
        ("cent/kWh", Decimal("0.001876")),
        ("EUR/MWh", Decimal("0.0001876")),
        ("€/MWh", Decimal("0.0001876")),
        ("€ / kWh", Decimal("0.1876")),
    ],
)
def test_price_to_per_kwh(unit, expected):
    assert price_to_per_kwh(Decimal("0.1876"), unit) == expected


def test_price_realistic_values():
    assert price_to_per_kwh(Decimal("18.76"), "c€/kWh") == Decimal("0.1876")
    assert price_to_per_kwh(Decimal("187.6"), "EUR/MWh") == Decimal("0.1876")


@pytest.mark.parametrize("unit", ["EUR", "kWh", "EUR/m3", "banana/kWh", None])
def test_price_unsupported(unit):
    assert not is_supported_price_unit(unit)
    with pytest.raises(UnsupportedUnitError):
        price_to_per_kwh(Decimal(1), unit)


def test_unsupported_energy_and_power():
    with pytest.raises(UnsupportedUnitError):
        energy_to_kwh(1, "J")
    with pytest.raises(UnsupportedUnitError):
        power_to_w(1, "VA")


def test_parsing_states():
    assert to_float("12.5") == 12.5
    assert to_float("unknown") is None
    assert to_float("nan") is None
    assert to_decimal("0.1405") == Decimal("0.1405")
    assert to_decimal("unavailable") is None
    assert to_decimal(None) is None
