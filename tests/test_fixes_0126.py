"""0.12.6: the end of a print is measured by the plug's energy meter, never estimated."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.fdm_printer_manager.coordinator import PrintCostCoordinator
from custom_components.fdm_printer_manager.models import ElectricityAccumulator, ElectricitySample, PrintJob

T0 = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)
PRICE = Decimal("0.19")


class Clock:
    """Fake time: asyncio.sleep moves it forward."""

    def __init__(self) -> None:
        self.now = T0

    def utcnow(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Reader:
    """Plug meter that steps after ``step_after`` seconds (never if None)."""

    def __init__(self, clock: Clock, start: float, step_after: float | None, step: float = 0.0002) -> None:
        self.clock, self.start, self.step_after, self.step = clock, start, step_after, step

    def sample(self, now: datetime) -> ElectricitySample:
        value = self.start
        if self.step_after is not None and (now - T0).total_seconds() >= self.step_after:
            value += self.step
        return ElectricitySample(now, power_w=60, energy_kwh=value, price_per_kwh=PRICE)


def _job_awaiting(meter: float) -> PrintJob:
    acc = ElectricityAccumulator()
    start = T0 - timedelta(minutes=4)
    acc.add_sample(ElectricitySample(start, power_w=300, energy_kwh=meter - 0.3, price_per_kwh=PRICE))
    acc.add_sample(ElectricitySample(T0 - timedelta(minutes=1), power_w=300, energy_kwh=meter, price_per_kwh=PRICE))
    acc.add_sample(ElectricitySample(T0, power_w=300, energy_kwh=meter, price_per_kwh=PRICE))  # finished
    assert acc.awaiting_meter
    return PrintJob(printer_id="p1", started_at=T0 - timedelta(minutes=60), electricity=acc, name="part")


async def _wait(job: PrintJob, reader: Reader, clock: Clock) -> None:
    coordinator = SimpleNamespace(reader=reader)
    with (
        patch("custom_components.fdm_printer_manager.coordinator.dt_util.utcnow", clock.utcnow),
        patch("custom_components.fdm_printer_manager.coordinator.asyncio.sleep", clock.sleep),
    ):
        await PrintCostCoordinator._async_wait_meter_step(coordinator, job)
    job.electricity.flush()


async def test_waits_for_the_plug_step_and_books_it() -> None:
    clock = Clock()
    job = _job_awaiting(12.0)
    await _wait(job, Reader(clock, 12.0, step_after=70), clock)
    assert job.energy_kwh == pytest.approx(0.3002)  # all from the meter
    assert job.electricity.unmetered_kwh == 0
    assert job.notes() == []
    assert clock.now - T0 < timedelta(seconds=80)  # stopped waiting at the step


async def test_plug_never_steps_nothing_is_estimated() -> None:
    clock = Clock()
    job = _job_awaiting(12.0)
    await _wait(job, Reader(clock, 12.0, step_after=None), clock)
    assert job.energy_kwh == pytest.approx(0.3)  # only measured energy
    assert job.electricity.unmetered_kwh > 0
    assert any("not measured and not counted" in note for note in job.notes())
    assert clock.now - T0 >= timedelta(seconds=300)


def test_meter_back_from_unavailable_rounded_down_is_not_a_reset() -> None:
    """Real case (job 7c576ea4, 07-11): 36.931773 -> unavailable -> 36.931 booked the whole meter (36.93 kWh)."""
    acc = ElectricityAccumulator()
    t = T0
    acc.add_sample(ElectricitySample(t, power_w=110, energy_kwh=36.492, price_per_kwh=PRICE))
    acc.add_sample(ElectricitySample(t + timedelta(hours=4), power_w=110, energy_kwh=36.931773, price_per_kwh=PRICE))
    acc.add_sample(
        ElectricitySample(t + timedelta(hours=4, minutes=1), power_w=None, energy_kwh=None, price_per_kwh=PRICE)
    )
    acc.add_sample(
        ElectricitySample(t + timedelta(hours=4, minutes=2), power_w=110, energy_kwh=36.931, price_per_kwh=PRICE)
    )
    assert acc.meter_resets == 0
    assert not acc.meter_stepped(36.931)
    acc.add_sample(ElectricitySample(t + timedelta(hours=10), power_w=110, energy_kwh=37.612, price_per_kwh=PRICE))
    acc.flush()
    assert acc.energy_kwh == pytest.approx(1.12)
    assert acc.meter_resets == 0


def test_real_reset_is_still_a_reset() -> None:
    acc = ElectricityAccumulator()
    acc.add_sample(ElectricitySample(T0, power_w=100, energy_kwh=36.9, price_per_kwh=PRICE))
    assert acc.meter_stepped(0.01)
    acc.add_sample(ElectricitySample(T0 + timedelta(minutes=10), power_w=100, energy_kwh=0.01, price_per_kwh=PRICE))
    assert acc.meter_resets == 1
    assert acc.energy_kwh == pytest.approx(0.01)
