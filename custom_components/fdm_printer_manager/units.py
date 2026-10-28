"""Unit normalisation.

Pure functions, no Home Assistant import, so they can be unit tested alone.

Internal units:
    power   -> W
    energy  -> kWh
    price   -> currency / kWh   (as Decimal)
    mass    -> g
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any


class UnsupportedUnitError(ValueError):
    """Raised when a unit cannot be converted."""


def _norm(unit: str | None) -> str:
    """Lower-case a unit and drop spaces so '€ / kWh' == '€/kwh'."""
    return (unit or "").replace(" ", "").lower()


# --------------------------------------------------------------------------- #
# Numbers
# --------------------------------------------------------------------------- #
def to_float(value: Any) -> float | None:
    """Parse a HA state into a float. Returns None for unknown/unavailable."""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):  # NaN / inf
        return None
    return result


def to_decimal(value: Any) -> Decimal | None:
    """Parse a HA state into a Decimal. Returns None for unknown/unavailable."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


# --------------------------------------------------------------------------- #
# Power
# --------------------------------------------------------------------------- #
_POWER_FACTORS_TO_W: dict[str, float] = {
    "w": 1.0,
    "kw": 1000.0,
    "mw": 1_000_000.0,
}


def power_to_w(value: float, unit: str | None) -> float:
    """Convert a power value to W."""
    factor = _POWER_FACTORS_TO_W.get(_norm(unit))
    if factor is None:
        raise UnsupportedUnitError(f"Unsupported power unit: {unit!r}")
    return value * factor


def w_to_kw(value_w: float) -> float:
    """Convert W to kW."""
    return value_w / 1000.0


def is_supported_power_unit(unit: str | None) -> bool:
    """Whether a power unit can be converted."""
    return _norm(unit) in _POWER_FACTORS_TO_W


# --------------------------------------------------------------------------- #
# Energy
# --------------------------------------------------------------------------- #
_ENERGY_FACTORS_TO_KWH: dict[str, float] = {
    "wh": 0.001,
    "kwh": 1.0,
    "mwh": 1000.0,
}


def energy_to_kwh(value: float, unit: str | None) -> float:
    """Convert an energy value to kWh."""
    factor = _ENERGY_FACTORS_TO_KWH.get(_norm(unit))
    if factor is None:
        raise UnsupportedUnitError(f"Unsupported energy unit: {unit!r}")
    return value * factor


def is_supported_energy_unit(unit: str | None) -> bool:
    """Whether an energy unit can be converted."""
    return _norm(unit) in _ENERGY_FACTORS_TO_KWH


# --------------------------------------------------------------------------- #
# Electricity price
# --------------------------------------------------------------------------- #
_CURRENCY_MAJOR = {"eur", "€", "euro", "euros", "usd", "$", "gbp", "£", "chf", ""}
_CURRENCY_MINOR = {"c€", "ct", "cent", "cents", "c", "centime", "centimes", "p", "¢", "eurocent"}
_ENERGY_DENOMINATORS = {"wh": Decimal("0.001"), "kwh": Decimal(1), "mwh": Decimal(1000)}


def price_to_per_kwh(value: Decimal, unit: str | None) -> Decimal:
    """Convert an electricity price to <currency>/kWh.

    Accepts e.g. ``EUR/kWh``, ``€/kWh``, ``c€/kWh``, ``cent/kWh``,
    ``EUR/MWh``, ``€/MWh``, ``EUR/Wh``.
    """
    unit_n = _norm(unit)
    if "/" not in unit_n:
        raise UnsupportedUnitError(f"Unsupported price unit: {unit!r}")
    numerator, denominator = unit_n.split("/", 1)

    energy_kwh = _ENERGY_DENOMINATORS.get(denominator)
    if energy_kwh is None:
        raise UnsupportedUnitError(f"Unsupported price unit: {unit!r}")

    if numerator in _CURRENCY_MINOR:
        major = Decimal("0.01")
    elif numerator in _CURRENCY_MAJOR:
        major = Decimal(1)
    else:
        raise UnsupportedUnitError(f"Unsupported price unit: {unit!r}")

    # price per <denominator> -> price per kWh
    return value * major / energy_kwh


def is_supported_price_unit(unit: str | None) -> bool:
    """Whether a price unit can be converted."""
    try:
        price_to_per_kwh(Decimal(1), unit)
    except UnsupportedUnitError:
        return False
    return True


# --------------------------------------------------------------------------- #
# Mass
# --------------------------------------------------------------------------- #
