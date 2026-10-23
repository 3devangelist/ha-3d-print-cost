"""Electricity accounting for a single print.

The accumulator receives samples ``(timestamp, power, energy meter, price)``
and integrates *energy x price* interval by interval, so a tariff change
during the print is always taken into account. It never multiplies the total
energy by the last known price.

Rules
-----
* Energy of an interval comes from the cumulative energy meter when one is
  configured and both ends of the interval are known (preferred, exact),
  otherwise from the power sensor (``left`` or ``trapezoidal`` rule).
* The price applied to an interval is the price that was valid at its start
  (prices are step functions). The coordinator samples on every price change,
  so intervals never straddle a tariff change by more than one sample.
* While the meter is momentarily unavailable the power estimate is kept
  *pending*; the next meter reading books the exact delta for the whole hole
  (priced like the estimate). ``flush()`` books the estimate if the meter
  never comes back before the end of the print.
* An interval with an unknown price adds energy but no cost; that energy is
  tracked in ``uncosted_energy_kwh`` and ``cost_complete`` becomes ``False``.
  Unknown is never silently turned into zero.

No Home Assistant import: fully unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..const import IntegrationMethod
from ..units import to_decimal

ZERO = Decimal(0)


@dataclass(slots=True)
class ElectricitySample:
    """One reading of the electrical inputs."""

    timestamp: datetime
    power_w: float | None = None
    energy_kwh: float | None = None  # cumulative meter value, already in kWh
    price_per_kwh: Decimal | None = None  # already normalised to <cur>/kWh


@dataclass(slots=True)
class ElectricityAccumulator:
    """Integrates energy and cost over the life of a print."""

    method: IntegrationMethod = IntegrationMethod.LEFT
    max_gap_s: float = 300.0

    energy_kwh: float = 0.0
    cost: Decimal = ZERO
    uncosted_energy_kwh: float = 0.0
    energy_start_kwh: float | None = None  # meter value at the first sample
    energy_end_kwh: float | None = None  # meter value at the last sample
    gap_seconds: float = 0.0  # time integrated across long holes
    meter_resets: int = 0

    last_timestamp: datetime | None = None
    last_power_w: float | None = None
    last_energy_kwh: float | None = None
    last_price: Decimal | None = None

    price_min: Decimal | None = None
    price_max: Decimal | None = None

    # Power estimate since the last meter reading, not booked yet.
    pending_kwh: float = 0.0
    pending_cost: Decimal = ZERO
    pending_uncosted_kwh: float = 0.0
    _pending_price_hint: Decimal | None = None
    _notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    @property
    def total_energy_kwh(self) -> float:
        """Booked energy plus the power estimate the meter has not confirmed yet."""
        return self.energy_kwh + self.pending_kwh

    @property
    def total_cost(self) -> Decimal:
        """Booked cost plus the cost of the pending estimate."""
        return self.cost + self.pending_cost

    @property
    def total_uncosted_energy_kwh(self) -> float:
        """Energy without a known price, pending estimate included."""
        return self.uncosted_energy_kwh + self.pending_uncosted_kwh

    @property
    def cost_complete(self) -> bool:
        """True when every kWh of the print has a known price."""
        return self.total_uncosted_energy_kwh <= 1e-12

    @property
    def notes(self) -> list[str]:
        """Human readable notes about approximations made."""
        return list(self._notes)

    # ------------------------------------------------------------------ #
    def add_sample(self, sample: ElectricitySample) -> None:
        """Integrate the interval since the previous sample, then store it."""
        if self.last_timestamp is None:
            self._store(sample)
            return

        dt_s = (sample.timestamp - self.last_timestamp).total_seconds()
        if dt_s < 0:
            # Clock went backwards: ignore the sample entirely.
            return
        if dt_s == 0:
            # Same instant: just refresh the "current" values (e.g. price
            # change and power change in the same tick).
            self._store(sample)
            return

        if dt_s > self.max_gap_s:
            self.gap_seconds += dt_s

        # Power-based estimate of the interval, priced at the price valid at
        # its start. It is the final value without a meter, and the price
        # weighting of the meter delta otherwise.
        est_kwh = self._power_energy(sample, dt_s)
        if est_kwh is not None and est_kwh > 0:
            self.pending_kwh += est_kwh
            if self.last_price is None:
                self.pending_uncosted_kwh += est_kwh
            else:
                self.pending_cost += Decimal(repr(est_kwh)) * self.last_price
        # Remember the price even when the power estimate is zero/unknown,
        # so a meter-only setup still prices its delta correctly.
        self._pending_price_hint = self.last_price

        meter_now = sample.energy_kwh
        meter_prev = self.last_energy_kwh
        if meter_prev is None:
            # No meter (or not yet seen): the power estimate is the value.
            self._commit_pending()
        elif meter_now is None:
            # Meter momentarily unavailable: keep the estimate pending, the
            # next meter reading will cover the whole hole exactly.
            self._note("energy meter temporarily unavailable, its next reading covered the hole")
        elif meter_now > meter_prev:
            self._commit_meter(meter_now - meter_prev)
        elif meter_now == meter_prev:
            # Meter not advanced yet (coarse resolution, or a plug that only
            # refreshes its counter every few minutes): keep the estimate
            # pending, the next step of the meter replaces it.
            pass
        else:
            # Meter reset (plug rebooted, daily counter...): use power.
            self.meter_resets += 1
            self._note("energy meter decreased during the print, power used for that interval")
            self._commit_pending()

        self._store(sample)

    def flush(self) -> None:
        """Commit a pending estimate (meter never came back before the end)."""
        if self.pending_kwh or self.pending_uncosted_kwh:
            self._note("energy meter did not report the last part of the print, power estimate used")
            self._commit_pending()

    # ------------------------------------------------------------------ #
    def _commit_pending(self) -> None:
        self.energy_kwh += self.pending_kwh
        self.cost += self.pending_cost
        self.uncosted_energy_kwh += self.pending_uncosted_kwh
        self._clear_pending()

    def _commit_meter(self, delta_kwh: float) -> None:
        """Book a meter delta, priced like the power estimate of the same span."""
        if delta_kwh > 0:
            self.energy_kwh += delta_kwh
            priced_est = self.pending_kwh - self.pending_uncosted_kwh
            if self.pending_kwh > 0:
                priced_share = priced_est / self.pending_kwh
                if priced_est > 0:
                    avg_price = self.pending_cost / Decimal(repr(priced_est))
                    self.cost += Decimal(repr(delta_kwh * priced_share)) * avg_price
                self.uncosted_energy_kwh += delta_kwh * (1 - priced_share)
            elif self._pending_price_hint is not None:
                self.cost += Decimal(repr(delta_kwh)) * self._pending_price_hint
            else:
                self.uncosted_energy_kwh += delta_kwh
        self._clear_pending()

    def _clear_pending(self) -> None:
        self.pending_kwh = 0.0
        self.pending_cost = ZERO
        self.pending_uncosted_kwh = 0.0

    def _power_energy(self, sample: ElectricitySample, dt_s: float) -> float | None:
        """Energy (kWh) between the previous sample and this one, from power."""
        hours = dt_s / 3600.0
        prev_w = self.last_power_w
        cur_w = sample.power_w
        if self.method is IntegrationMethod.TRAPEZOIDAL and prev_w is not None and cur_w is not None:
            avg_w = (prev_w + cur_w) / 2.0
        elif prev_w is not None:
            avg_w = prev_w
        elif cur_w is not None and dt_s <= self.max_gap_s:
            avg_w = cur_w
        else:
            return None
        return max(avg_w, 0.0) / 1000.0 * hours

    def _store(self, sample: ElectricitySample) -> None:
        self.last_timestamp = sample.timestamp
        # Keep the last known value when a reading is momentarily unknown,
        # except for the price: an unknown price must stay unknown.
        if sample.power_w is not None:
            self.last_power_w = sample.power_w
        if sample.energy_kwh is not None:
            self.last_energy_kwh = sample.energy_kwh
            self.energy_end_kwh = sample.energy_kwh
            if self.energy_start_kwh is None:
                self.energy_start_kwh = sample.energy_kwh
        self.last_price = sample.price_per_kwh
        if sample.price_per_kwh is not None:
            p = sample.price_per_kwh
            self.price_min = p if self.price_min is None else min(self.price_min, p)
            self.price_max = p if self.price_max is None else max(self.price_max, p)

    def _note(self, text: str) -> None:
        if text not in self._notes:
            self._notes.append(text)

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def as_dict(self) -> dict[str, Any]:
        """Serialise to JSON-compatible data."""
        return {
            "method": str(self.method),
            "max_gap_s": self.max_gap_s,
            "energy_kwh": self.energy_kwh,
            "cost": str(self.cost),
            "uncosted_energy_kwh": self.uncosted_energy_kwh,
            "energy_start_kwh": self.energy_start_kwh,
            "energy_end_kwh": self.energy_end_kwh,
            "gap_seconds": self.gap_seconds,
            "meter_resets": self.meter_resets,
            "last_timestamp": self.last_timestamp.isoformat() if self.last_timestamp else None,
            "last_power_w": self.last_power_w,
            "last_energy_kwh": self.last_energy_kwh,
            "last_price": str(self.last_price) if self.last_price is not None else None,
            "price_min": str(self.price_min) if self.price_min is not None else None,
            "price_max": str(self.price_max) if self.price_max is not None else None,
            "pending_kwh": self.pending_kwh,
            "pending_cost": str(self.pending_cost),
            "pending_uncosted_kwh": self.pending_uncosted_kwh,
            "notes": list(self._notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ElectricityAccumulator:
        """Restore from :meth:`as_dict` output."""
        last_ts = data.get("last_timestamp")
        acc = cls(
            method=IntegrationMethod(data.get("method", IntegrationMethod.LEFT)),
            max_gap_s=float(data.get("max_gap_s", 300.0)),
            energy_kwh=float(data.get("energy_kwh", 0.0)),
            cost=to_decimal(data.get("cost")) or ZERO,
            uncosted_energy_kwh=float(data.get("uncosted_energy_kwh", 0.0)),
            energy_start_kwh=data.get("energy_start_kwh"),
            energy_end_kwh=data.get("energy_end_kwh"),
            gap_seconds=float(data.get("gap_seconds", 0.0)),
            meter_resets=int(data.get("meter_resets", 0)),
            last_timestamp=datetime.fromisoformat(last_ts) if last_ts else None,
            last_power_w=data.get("last_power_w"),
            last_energy_kwh=data.get("last_energy_kwh"),
            last_price=to_decimal(data.get("last_price")),
            price_min=to_decimal(data.get("price_min")),
            price_max=to_decimal(data.get("price_max")),
            pending_kwh=float(data.get("pending_kwh", 0.0)),
            pending_cost=to_decimal(data.get("pending_cost")) or ZERO,
            pending_uncosted_kwh=float(data.get("pending_uncosted_kwh", 0.0)),
        )
        acc._notes = list(data.get("notes", []))
        return acc
