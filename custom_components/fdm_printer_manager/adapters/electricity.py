"""Read the smart plug and the electricity price from Home Assistant states.

Everything is normalised here (W, kWh, <currency>/kWh) so the accounting code
never sees a raw unit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from ..models import ElectricitySample
from ..units import (
    UnsupportedUnitError,
    energy_to_kwh,
    is_supported_energy_unit,
    is_supported_power_unit,
    is_supported_price_unit,
    power_to_w,
    price_to_per_kwh,
    to_decimal,
    to_float,
)

_LOGGER = logging.getLogger(__name__)

_INVALID = {None, "", STATE_UNKNOWN, STATE_UNAVAILABLE}


@dataclass(slots=True)
class ElectricitySources:
    """Entity ids of the electrical inputs."""

    power_entity: str | None
    energy_entity: str | None
    price_entity: str | None
    price_fixed: float | None = None

    @property
    def measures_energy(self) -> bool:
        """A power sensor or an energy meter is configured."""
        return bool(self.power_entity or self.energy_entity)


class ElectricityReader:
    """Builds :class:`ElectricitySample` objects from HA states."""

    def __init__(self, hass: HomeAssistant, sources: ElectricitySources) -> None:
        self._hass = hass
        self.sources = sources
        self._warned: set[str] = set()

    def sample(self, now: datetime) -> ElectricitySample:
        """Read all inputs at ``now``."""
        return ElectricitySample(
            timestamp=now,
            power_w=self.power_w(),
            energy_kwh=self.energy_kwh(),
            price_per_kwh=self.price_per_kwh(),
        )

    # ------------------------------------------------------------------ #
    def _state(self, entity_id: str | None):
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if state is None or state.state in _INVALID:
            return None
        return state

    def power_w(self) -> float | None:
        """Instant power in W."""
        state = self._state(self.sources.power_entity)
        if state is None:
            return None
        value = to_float(state.state)
        if value is None:
            return None
        try:
            return power_to_w(value, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) or "W")
        except UnsupportedUnitError as err:
            self._warn_once("power", str(err))
            return None

    def energy_kwh(self) -> float | None:
        """Cumulative meter value in kWh."""
        state = self._state(self.sources.energy_entity)
        if state is None:
            return None
        value = to_float(state.state)
        if value is None:
            return None
        try:
            return energy_to_kwh(value, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) or "kWh")
        except UnsupportedUnitError as err:
            self._warn_once("energy", str(err))
            return None

    def price_per_kwh(self):
        """Current price in <currency>/kWh (Decimal): the price entity, else the typed price."""
        state = self._state(self.sources.price_entity)
        if state is None:
            if not self.sources.price_entity and self.sources.price_fixed is not None:
                return to_decimal(str(self.sources.price_fixed))
            return None
        value = to_decimal(state.state)
        if value is None:
            return None
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        try:
            return price_to_per_kwh(value, unit or "EUR/kWh")
        except UnsupportedUnitError as err:
            self._warn_once("price", str(err))
            return None

    def _warn_once(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            _LOGGER.warning(message)


def validate_units(hass: HomeAssistant, power: str | None, energy: str | None, price: str | None) -> dict[str, str]:
    """Config-flow validation. Returns ``{field: error_key}``."""
    errors: dict[str, str] = {}

    def unit_of(entity_id: str) -> str | None:
        state = hass.states.get(entity_id)
        return state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) if state else None

    if power and (u := unit_of(power)) is not None and not is_supported_power_unit(u):
        errors["power_entity"] = "unsupported_power_unit"
    if energy and (u := unit_of(energy)) is not None and not is_supported_energy_unit(u):
        errors["energy_entity"] = "unsupported_energy_unit"
    if price and (u := unit_of(price)) is not None and not is_supported_price_unit(u):
        errors["price_entity"] = "unsupported_price_unit"
    return errors
