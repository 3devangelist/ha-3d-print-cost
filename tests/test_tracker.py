"""Print life cycle: start, update, end, duplicates, restart recovery."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from custom_components.printer_manager.models import (
    ElectricitySample,
    JobPhase,
    PrintJob,
    PrintResult,
)
from custom_components.printer_manager.tracker import (
    PrinterActivity,
    PrinterSnapshot,
    PrintTracker,
    TransitionKind,
    classify_status,
)

T0 = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
PRICE = Decimal("0.20")


def snap(minute: float, status: str | None, *, job="Flower Lamp", watts=500.0, meter=None, price=PRICE, **kw):
    ts = T0 + timedelta(minutes=minute)
    return PrinterSnapshot(
        timestamp=ts,
        status=status,
        job_name=job,
        electricity=ElectricitySample(ts, power_w=watts, energy_kwh=meter, price_per_kwh=price),
        **kw,
    )


def kinds(transitions):
    return [t.kind for t in transitions]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("prepare", PrinterActivity.ACTIVE),
        ("running", PrinterActivity.ACTIVE),
        ("pause", PrinterActivity.ACTIVE),
        ("finish", PrinterActivity.SUCCESS),
        ("failed", PrinterActivity.FAILED),
        ("CANCEL", PrinterActivity.CANCELLED),
        ("idle", PrinterActivity.IDLE),
        ("offline", PrinterActivity.UNKNOWN),
        ("unavailable", PrinterActivity.UNKNOWN),
        ("slicing", PrinterActivity.UNKNOWN),
        (None, PrinterActivity.UNKNOWN),
    ],
)
def test_classify(raw, expected):
    assert classify_status(raw) is expected


def test_full_print_cycle():
    tracker = PrintTracker("p1")
    assert tracker.process(snap(0, "idle")) == []

    started = tracker.process(snap(1, "prepare"))
    assert kinds(started) == [TransitionKind.STARTED]
    job = started[0].job
    assert job.started_at == T0 + timedelta(minutes=1)
    assert job.electricity_cost == 0

    assert kinds(tracker.process(snap(5, "running", progress=10))) == [TransitionKind.UPDATED]
    assert kinds(tracker.process(snap(30, "pause"))) == [TransitionKind.UPDATED]
    assert kinds(tracker.process(snap(61, "running", progress=100))) == [TransitionKind.UPDATED]

    ended = tracker.process(snap(61, "finish"))
    assert kinds(ended) == [TransitionKind.ENDED]
    assert ended[0].job is job
    assert job.result is PrintResult.SUCCESS
    assert job.phase is JobPhase.FINALIZING
    assert job.energy_kwh == pytest.approx(0.5)  # 500 W x 1 h
    assert job.electricity_cost == pytest.approx(Decimal("0.10"))
    assert job.progress == 100
    assert tracker.active_job is None

    # "finish" stays displayed by the printer: no new job, no duplicate end
    assert tracker.process(snap(70, "finish")) == []


def test_repeated_states_do_not_duplicate_jobs():
    tracker = PrintTracker("p1")
    first = tracker.process(snap(0, "prepare"))[0].job
    for minute in range(1, 20):
        transitions = tracker.process(snap(minute, "running" if minute % 2 else "prepare"))
        assert kinds(transitions) == [TransitionKind.UPDATED]
        assert transitions[0].job is first


def test_offline_does_not_end_the_print():
    tracker = PrintTracker("p1")
    tracker.process(snap(0, "running"))
    assert kinds(tracker.process(snap(5, "offline"))) == [TransitionKind.UPDATED]
    assert kinds(tracker.process(snap(6, "unavailable"))) == [TransitionKind.UPDATED]
    assert tracker.active_job is not None


@pytest.mark.parametrize(
    ("status", "result"),
    [("failed", PrintResult.FAILED), ("cancel", PrintResult.CANCELLED), ("idle", PrintResult.UNKNOWN)],
)
def test_terminal_results(status, result):
    tracker = PrintTracker("p1")
    tracker.process(snap(0, "running"))
    ended = tracker.process(snap(10, status))
    assert ended[0].job.result is result


def test_new_job_without_terminal_state():
    tracker = PrintTracker("p1")
    old = tracker.process(snap(0, "running", job="A"))[0].job
    transitions = tracker.process(snap(30, "prepare", job="B"))
    assert kinds(transitions) == [TransitionKind.ENDED, TransitionKind.STARTED]
    assert transitions[0].job is old
    assert old.result is PrintResult.UNKNOWN
    assert transitions[1].job.name == "B"


def test_job_name_learned_later():
    tracker = PrintTracker("p1")
    job = tracker.process(snap(0, "prepare", job=None))[0].job
    tracker.process(snap(1, "running", job="Benchy"))
    assert job.name == "Benchy"


def test_restart_restores_active_job_with_meter():
    """HA restarts at minute 30, printer still running: nothing is lost."""
    tracker = PrintTracker("p1")
    tracker.process(snap(0, "running", meter=10.0))
    tracker.process(snap(30, "running", meter=10.25))
    saved = tracker.active_job.as_dict()

    # --- HA restart: new tracker, job restored from storage ---
    tracker2 = PrintTracker("p1")
    tracker2.restore(PrintJob.from_dict(saved))
    # HA was down 20 min; the meter covers the hole exactly
    tracker2.process(snap(50, "unavailable", watts=None, meter=None))  # entities not loaded yet
    tracker2.process(snap(55, "running", meter=10.45))
    ended = tracker2.process(snap(60, "finish", meter=10.5))

    job = ended[0].job
    assert job.job_id == saved["job_id"]
    assert job.started_at == T0
    assert job.energy_kwh == pytest.approx(0.5)
    assert job.electricity_cost == pytest.approx(Decimal("0.10"))
    assert job.result is PrintResult.SUCCESS


def test_restart_after_print_finished_while_down():
    tracker = PrintTracker("p1")
    tracker.process(snap(0, "running"))
    saved = tracker.active_job.as_dict()
    tracker2 = PrintTracker("p1")
    tracker2.restore(PrintJob.from_dict(saved))
    ended = tracker2.process(snap(90, "finish"))
    assert ended[0].job.result is PrintResult.SUCCESS
    assert ended[0].job.electricity.gap_seconds == 90 * 60


def test_restore_ignores_completed_job():
    tracker = PrintTracker("p1")
    job = tracker.process(snap(0, "running"))[0].job
    job.phase = JobPhase.COMPLETED
    tracker2 = PrintTracker("p1")
    tracker2.restore(job)
    assert tracker2.active_job is None
