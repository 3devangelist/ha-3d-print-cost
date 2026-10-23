"""Filament pricing, HaspelSync parsing and the resolver."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from custom_components.ha_3d_print_cost.adapters.haspelsync import (
    parse_print_usage,
    parse_printers,
    parse_slots,
)
from custom_components.ha_3d_print_cost.adapters.spoolman import (
    SpoolmanError,
    SpoolNotFoundError,
    parse_setting_value,
    parse_spool,
)
from custom_components.ha_3d_print_cost.const import FilamentFallback
from custom_components.ha_3d_print_cost.filament import (
    FilamentResolver,
    apply_outcome,
    bambu_slot_to_label,
)
from custom_components.ha_3d_print_cost.models import (
    ElectricityAccumulator,
    FilamentStatus,
    FilamentUsage,
    PriceSource,
    PrintJob,
    PrintResult,
    SpoolInfo,
    UsageSource,
    price_per_gram,
    summarize_usages,
)

T0 = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Price normalisation
# --------------------------------------------------------------------------- #
def test_20_eur_per_kg_50_g_is_1_eur():
    spool = SpoolInfo(spool_id=1, spool_price=Decimal(20), initial_weight_g=1000)
    usage = FilamentUsage(weight_g=50, source=UsageSource.HASPELSYNC, spool_id=1)
    usage.apply_price(spool)
    assert usage.cost == Decimal(1)


def test_case_a_spool_price():
    unit = price_per_gram(SpoolInfo(spool_id=1, spool_price=Decimal(22), initial_weight_g=1000))
    assert unit.per_gram == Decimal("0.022")
    assert unit.source is PriceSource.SPOOL


def test_spool_price_without_initial_weight_uses_filament_weight():
    unit = price_per_gram(SpoolInfo(spool_id=1, spool_price=Decimal(25), filament_weight_g=500))
    assert unit.per_gram == Decimal("0.05")


def test_filament_catalogue_price_fallback():
    unit = price_per_gram(SpoolInfo(spool_id=1, filament_price=Decimal(18), filament_weight_g=1000))
    assert unit.per_gram == Decimal("0.018")
    assert unit.source is PriceSource.FILAMENT


def test_case_b_explicit_unit_price_wins():
    spool = SpoolInfo(spool_id=1, spool_price=Decimal(99), initial_weight_g=1000, price_per_kg_extra=Decimal(20))
    unit = price_per_gram(spool)
    assert unit.per_gram == Decimal("0.02")
    assert unit.source is PriceSource.EXTRA_PER_KG


def test_no_price_is_none_not_zero():
    assert price_per_gram(SpoolInfo(spool_id=1, initial_weight_g=1000)) is None
    assert price_per_gram(SpoolInfo(spool_id=1, spool_price=Decimal(20))) is None  # no weight


def test_several_spools_sum():
    a = FilamentUsage(weight_g=62.4, source=UsageSource.HASPELSYNC, spool_id=23)
    b = FilamentUsage(weight_g=22.2, source=UsageSource.HASPELSYNC, spool_id=45)
    a.apply_price(SpoolInfo(spool_id=23, spool_price=Decimal("19.39"), initial_weight_g=1000))
    b.apply_price(SpoolInfo(spool_id=45, spool_price=Decimal("22.97"), initial_weight_g=1000))
    totals = summarize_usages([a, b])
    assert totals.weight_g == pytest.approx(84.6)
    assert totals.cost == a.cost + b.cost
    assert round(totals.cost, 2) == Decimal("1.72")
    assert totals.cost_complete


def test_one_unpriced_spool_makes_total_unknown():
    a = FilamentUsage(weight_g=10, source=UsageSource.HASPELSYNC, spool_id=1)
    b = FilamentUsage(weight_g=10, source=UsageSource.HASPELSYNC, spool_id=2)
    a.apply_price(SpoolInfo(spool_id=1, spool_price=Decimal(20), initial_weight_g=1000))
    b.apply_price(SpoolInfo(spool_id=2))
    totals = summarize_usages([a, b])
    assert totals.cost is None
    assert totals.weight_g == 20
    assert not totals.cost_complete


# --------------------------------------------------------------------------- #
# Spoolman parsing
# --------------------------------------------------------------------------- #
SPOOL_PAYLOAD = {
    "id": 23,
    "price": 19.99,
    "initial_weight": 1000,
    "remaining_weight": 512.3,
    "archived": False,
    "extra": {},
    "filament": {
        "id": 7,
        "name": "PLA Matte Black",
        "material": "PLA",
        "price": 24.99,
        "weight": 1000,
        "color_hex": "000000",
        "vendor": {"id": 1, "name": "Bambu Lab"},
        "extra": {"price_per_kg": '"18.5"'},
    },
}


def test_parse_spool():
    spool = parse_spool(SPOOL_PAYLOAD)
    assert spool.spool_id == 23
    assert spool.vendor == "Bambu Lab"
    assert spool.color_hex == "#000000"
    assert spool.spool_price == Decimal("19.99")
    assert spool.price_per_kg_extra == Decimal("18.5")
    assert spool.remaining_weight_g == 512.3


def test_parse_setting_value():
    assert parse_setting_value({"key": "currency", "value": '"EUR"'}) == "EUR"
    assert parse_setting_value({"value": '""'}) is None


# --------------------------------------------------------------------------- #
# HaspelSync parsing (payload shapes from its OpenAPI / source)
# --------------------------------------------------------------------------- #
def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def print_payload(*, state="FINISH", job="Flower Lamp", ended=T0 + timedelta(hours=3), rows=None, booked=True):
    return {
        "gcodeState": state,
        "consumptionBooked": booked,
        "lastPrintSummary": {
            "state": state,
            "jobName": job,
            "startedAt": ms(T0),
            "endedAt": ms(ended),
            "rows": rows
            if rows is not None
            else [
                {
                    "amsId": "A1",
                    "grams": 62.4,
                    "status": "booked",
                    "spoolId": 23,
                    "material": "PLA",
                    "color": "000000FF",
                    "spoolName": "PLA Matte Black",
                    "vendor": "Bambu Lab",
                },
                {"amsId": "A2", "grams": 22.2, "status": "booked", "spoolId": 45, "material": "PLA"},
                {"amsId": "A3", "grams": 0, "status": "unused", "spoolId": None},
            ],
            "note": None,
        },
    }


def test_parse_printers_and_slots():
    assert parse_printers([{"id": "SERIAL1", "name": "X2D"}])[0].printer_id == "SERIAL1"
    slots = parse_slots(
        [
            {"amsId": "A1", "spoolmanId": 23, "material": "PLA", "slot": {"tray_color": "FF0000FF"}},
            {"amsId": "B4", "spoolmanId": None, "slot": {}},
            {"amsId": "External", "existingSpool": {"id": 9, "filament": {"material": "PETG"}}},
        ]
    )
    assert slots["A1"].spool_id == 23
    assert slots["A1"].color == "#FF0000"
    assert slots["B4"].spool_id is None
    assert slots["External"].spool_id == 9


def test_parse_usage_success():
    usage = parse_print_usage(print_payload(), job_name="Flower Lamp", started_at=T0)
    assert usage is not None
    assert usage.result is PrintResult.SUCCESS
    assert [(u.slot, u.spool_id, u.weight_g) for u in usage.usages] == [("A1", 23, 62.4), ("A2", 45, 22.2)]
    assert usage.usages[0].color == "#000000"


def test_parse_usage_cancelled_uses_haspelsync_grams():
    payload = print_payload(state="CANCEL", rows=[{"amsId": "A1", "grams": 12.3, "status": "booked", "spoolId": 23}])
    usage = parse_print_usage(payload, job_name="Flower Lamp", started_at=T0)
    assert usage.result is PrintResult.CANCELLED
    assert usage.usages[0].weight_g == 12.3


def test_parse_usage_ignores_older_print_and_other_job():
    old = print_payload(ended=T0 - timedelta(hours=1))
    assert parse_print_usage(old, job_name="Flower Lamp", started_at=T0) is None
    assert parse_print_usage(print_payload(job="Other"), job_name="Flower Lamp", started_at=T0) is None
    not_booked = print_payload(rows=[], booked=False)
    assert parse_print_usage(not_booked, job_name="Flower Lamp", started_at=T0) is None


def test_parse_usage_unassigned_rows_are_kept_without_spool():
    payload = print_payload(rows=[{"amsId": "A4", "grams": 5.0, "status": "skipped"}])
    usage = parse_print_usage(payload, job_name="Flower Lamp", started_at=T0)
    assert usage.usages[0].spool_id is None
    assert usage.unbooked == ["A4: skipped"]


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("AMS 1 Tray 1", "A1"),
        ("AMS 2 Tray 4", "B4"),
        ("AMS 4 Tray 2", "D2"),
        ("AMS HT 1", "HT-A"),
        ("External Spool", "External"),
        ("External Spool 2", "External-2"),
        ("weird", None),
    ],
)
def test_bambu_slot_labels(name, label):
    assert bambu_slot_to_label(name) == label


# --------------------------------------------------------------------------- #
# Resolver
# --------------------------------------------------------------------------- #
class FakeHaspelsync:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    async def get_print_usage(self, printer_id, *, job_name, started_at):
        self.calls += 1
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, Exception):
            raise answer
        return parse_print_usage(answer, job_name=job_name, started_at=started_at) if answer else None


class FakeSpoolman:
    def __init__(self, spools, *, down=False):
        self.spools = spools
        self.down = down
        self.calls = 0

    async def get_spool(self, spool_id):
        self.calls += 1
        if self.down:
            raise SpoolmanError("timeout")
        if spool_id not in self.spools:
            raise SpoolNotFoundError(str(spool_id))
        return self.spools[spool_id]


SPOOLS = {
    23: SpoolInfo(spool_id=23, spool_price=Decimal(20), initial_weight_g=1000, material="PLA"),
    45: SpoolInfo(spool_id=45, spool_price=Decimal(25), initial_weight_g=1000, material="PLA"),
}


def make_job(result=PrintResult.SUCCESS, **kw) -> PrintJob:
    job = PrintJob(printer_id="p1", started_at=T0, electricity=ElectricityAccumulator(), name="Flower Lamp", **kw)
    job.result = result
    return job


async def test_resolver_uses_haspelsync_and_spoolman():
    hs = FakeHaspelsync([print_payload()])
    resolver = FilamentResolver(
        haspelsync=hs, haspelsync_printer_id="S", spoolman=FakeSpoolman(SPOOLS), fallback=FilamentFallback.NONE
    )
    job = make_job()
    outcome = await resolver.resolve(job, last_attempt=False)
    assert outcome.done
    assert outcome.status is FilamentStatus.RESOLVED
    apply_outcome(job, outcome)
    assert job.filament_weight_g == pytest.approx(84.6)
    assert job.filament_cost == Decimal(repr(62.4)) * Decimal("0.02") + Decimal(repr(22.2)) * Decimal("0.025")


async def test_resolver_waits_for_haspelsync_then_succeeds():
    hs = FakeHaspelsync([None, print_payload()])
    resolver = FilamentResolver(
        haspelsync=hs, haspelsync_printer_id="S", spoolman=FakeSpoolman(SPOOLS), fallback=FilamentFallback.NONE
    )
    job = make_job()
    first = await resolver.resolve(job, last_attempt=False)
    assert not first.done
    assert first.status is FilamentStatus.PENDING
    second = await resolver.resolve(job, last_attempt=False)
    assert second.done


async def test_cancelled_print_takes_haspelsync_value_and_result():
    payload = print_payload(state="CANCEL", rows=[{"amsId": "A1", "grams": 12.3, "status": "booked", "spoolId": 23}])
    resolver = FilamentResolver(
        haspelsync=FakeHaspelsync([payload]),
        haspelsync_printer_id="S",
        spoolman=FakeSpoolman(SPOOLS),
        fallback=FilamentFallback.PLANNED_PROGRESS,
    )
    job = make_job(result=PrintResult.FAILED, planned_weight_g=200, progress=50)
    outcome = await resolver.resolve(job, last_attempt=False)
    apply_outcome(job, outcome)
    assert job.result is PrintResult.CANCELLED
    assert job.filament_weight_g == 12.3  # not 200 x 50 %
    assert job.filament_usages[0].source is UsageSource.HASPELSYNC


async def test_fallback_planned_slots_on_last_attempt():
    resolver = FilamentResolver(
        haspelsync=FakeHaspelsync([]),
        haspelsync_printer_id="S",
        spoolman=FakeSpoolman(SPOOLS),
        fallback=FilamentFallback.PLANNED_SUCCESS,
    )
    job = make_job(planned_slot_weights={"AMS 1 Tray 1": 50.0, "AMS 1 Tray 2": 10.0}, slot_spools={"A1": 23, "A2": 45})
    outcome = await resolver.resolve(job, last_attempt=True)
    apply_outcome(job, outcome)
    assert job.filament_status is FilamentStatus.RESOLVED
    assert job.filament_cost == Decimal(1) + Decimal("0.25")
    assert all(u.source is UsageSource.BAMBU_SLOT for u in job.filament_usages)
    assert "planned" in job.filament_note


async def test_no_estimate_for_failed_print_by_default():
    resolver = FilamentResolver(
        haspelsync=None,
        haspelsync_printer_id=None,
        spoolman=FakeSpoolman(SPOOLS),
        fallback=FilamentFallback.PLANNED_SUCCESS,
    )
    job = make_job(result=PrintResult.FAILED, planned_weight_g=100, progress=40)
    outcome = await resolver.resolve(job, last_attempt=True)
    assert outcome.status is FilamentStatus.UNAVAILABLE
    assert outcome.usages == []


async def test_progress_estimate_only_when_enabled():
    resolver = FilamentResolver(
        haspelsync=None,
        haspelsync_printer_id=None,
        spoolman=FakeSpoolman(SPOOLS),
        fallback=FilamentFallback.PLANNED_PROGRESS,
    )
    job = make_job(result=PrintResult.FAILED, planned_weight_g=100, progress=40, slot_spools={"A1": 23})
    outcome = await resolver.resolve(job, last_attempt=True)
    assert outcome.usages[0].weight_g == pytest.approx(40)
    assert outcome.usages[0].source is UsageSource.ESTIMATED_PROGRESS
    assert outcome.usages[0].spool_id == 23  # single spool loaded -> attributable


async def test_total_weight_with_several_spools_has_unknown_cost():
    resolver = FilamentResolver(
        haspelsync=None,
        haspelsync_printer_id=None,
        spoolman=FakeSpoolman(SPOOLS),
        fallback=FilamentFallback.PLANNED_SUCCESS,
    )
    job = make_job(planned_weight_g=80, slot_spools={"A1": 23, "A2": 45})
    outcome = await resolver.resolve(job, last_attempt=True)
    apply_outcome(job, outcome)
    assert job.filament_weight_g == 80
    assert job.filament_cost is None
    assert job.filament_status is FilamentStatus.PARTIAL


async def test_spoolman_down_keeps_weight_but_no_cost():
    resolver = FilamentResolver(
        haspelsync=FakeHaspelsync([print_payload()]),
        haspelsync_printer_id="S",
        spoolman=FakeSpoolman(SPOOLS, down=True),
        fallback=FilamentFallback.NONE,
    )
    job = make_job()
    outcome = await resolver.resolve(job, last_attempt=False)
    apply_outcome(job, outcome)
    assert job.filament_weight_g == pytest.approx(84.6)
    assert job.filament_cost is None
    assert job.total_cost is None  # unknown is never turned into 0
    assert job.partial_total_cost is not None
    assert "Spoolman unavailable" in job.filament_note
    assert resolver._spoolman.calls == 1  # stops hammering a dead server


async def test_haspelsync_down_falls_back_on_last_attempt():
    resolver = FilamentResolver(
        haspelsync=FakeHaspelsync([HaspelsyncDown(), HaspelsyncDown()]),
        haspelsync_printer_id="S",
        spoolman=FakeSpoolman(SPOOLS),
        fallback=FilamentFallback.PLANNED_SUCCESS,
    )
    job = make_job(planned_slot_weights={"AMS 1 Tray 1": 50.0}, slot_spools={"A1": 23})
    assert not (await resolver.resolve(job, last_attempt=False)).done
    outcome = await resolver.resolve(job, last_attempt=True)
    assert outcome.done
    assert outcome.usages[0].cost == Decimal(1)
    assert "HaspelSync unavailable" in outcome.note


class HaspelsyncDown(Exception):
    pass


def test_total_cost_rules():
    job = make_job()
    job.electricity.cost = Decimal("0.13")
    job.filament_status = FilamentStatus.DISABLED
    assert job.total_cost == Decimal("0.13")  # electricity-only mode
    job.filament_status = FilamentStatus.RESOLVED
    job.filament_usages = [FilamentUsage(weight_g=10, source=UsageSource.HASPELSYNC, cost=Decimal("0.2"))]
    assert job.total_cost == Decimal("0.33")


def test_print_job_round_trip():
    job = make_job(planned_slot_weights={"AMS 1 Tray 1": 5.0}, slot_spools={"A1": 23})
    job.filament_usages = [FilamentUsage(weight_g=10, source=UsageSource.HASPELSYNC, spool_id=23, cost=Decimal("0.2"))]
    restored = PrintJob.from_dict(job.as_dict())
    assert restored.as_dict() == job.as_dict()
