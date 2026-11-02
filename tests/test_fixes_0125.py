"""0.12.5: the printer's energy / cost counters (``total_increasing``) never go down.

Seen on the Centauri Carbon: the plug counts in 0.2 Wh steps every one or two
minutes while the power estimate between steps runs a little ahead (7.4 W for
2 min = 0.25 Wh), so each meter step pulled ``energy_total`` back by ~0.04 Wh
and the recorder warned that the state "is not strictly increasing".
"""

import ast
import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, mock_restore_cache_with_extra_data

from custom_components.fdm_printer_manager.adapters.moonraker import MoonrakerClient, MoonrakerConnectionError
from custom_components.fdm_printer_manager.models import ElectricityAccumulator, ElectricitySample

from .test_integration import ENERGY, POWER, make_entry, set_states

T0 = datetime(2026, 11, 2, 12, 10, tzinfo=UTC)
PRICE = Decimal("0.19")
STEP_KWH = 0.0002075  # one step of the Centauri's plug


def _plug_like_centauri(acc: ElectricityAccumulator, minutes: int) -> list[float]:
    """7.4 W idle, sampled every 5 s; the meter steps 0.2075 Wh at the start of each 1st / 2nd minute."""
    meter = 33.819011
    shown: list[float] = []
    step_minutes = [1, 2] * minutes
    minute = 0
    for gap in step_minutes:
        for second in range(0, gap * 60, 5):
            ts = T0 + timedelta(minutes=minute, seconds=second)
            acc.add_sample(ElectricitySample(ts, power_w=7.4, energy_kwh=meter, price_per_kwh=PRICE))
            shown.append(acc.reported_energy_kwh)
        minute += gap
        if minute >= minutes:
            break
        meter = round(meter + STEP_KWH, 6)
    return shown


def test_meter_step_smaller_than_the_estimate_does_not_go_down():
    """Reproduces the drop: the raw total falls at each meter step, the reported one never does."""
    acc = ElectricityAccumulator()
    raw: list[float] = []
    shown: list[float] = []
    meter = 33.819011
    for minute in range(0, 20):
        if minute % 3 in (1, 0) and minute:  # steps after 1 then 2 minutes
            meter = round(meter + STEP_KWH, 6)
        for second in range(0, 60, 5):
            ts = T0 + timedelta(minutes=minute, seconds=second)
            acc.add_sample(ElectricitySample(ts, power_w=7.4, energy_kwh=meter, price_per_kwh=PRICE))
            raw.append(acc.total_energy_kwh)
            shown.append(acc.reported_energy_kwh)

    assert any(b < a for a, b in itertools.pairwise(raw)), "the raw total should show the old drop"
    assert all(b >= a for a, b in itertools.pairwise(shown))
    # Rounded like the sensor (6 decimals): still never down.
    rounded = [round(v, 6) for v in shown]
    assert all(b >= a for a, b in itertools.pairwise(rounded))


def test_reported_catches_up_with_the_meter_and_goes_on():
    acc = ElectricityAccumulator()
    shown = _plug_like_centauri(acc, 60)
    assert all(b >= a for a, b in itertools.pairwise(shown))
    # No drift: what is shown stays within one meter step of what the meter counted.
    assert acc.reported_energy_kwh - acc.energy_kwh <= 2 * STEP_KWH
    assert acc.reported_cost >= acc.total_cost
    assert acc.reported_cost == pytest.approx(Decimal(repr(acc.reported_energy_kwh)) * PRICE, abs=Decimal("1e-6"))


def test_cost_never_goes_down_either():
    acc = ElectricityAccumulator()
    costs: list[Decimal] = []
    meter = 10.0
    for minute in range(0, 12):
        if minute and minute % 2 == 0:
            meter = round(meter + STEP_KWH, 7)
        for second in range(0, 60, 5):
            ts = T0 + timedelta(minutes=minute, seconds=second)
            acc.add_sample(ElectricitySample(ts, power_w=7.4, energy_kwh=meter, price_per_kwh=PRICE))
            costs.append(acc.reported_cost)
    assert all(b >= a for a, b in itertools.pairwise(costs))


def test_plug_counter_reset_adds_the_count_from_zero():
    """Meter only (no power sensor): the plug restarts its counter, the total keeps accumulating."""
    acc = ElectricityAccumulator()
    acc.add_sample(ElectricitySample(T0, energy_kwh=5.0, price_per_kwh=PRICE))
    acc.add_sample(ElectricitySample(T0 + timedelta(minutes=10), energy_kwh=5.1, price_per_kwh=PRICE))
    acc.add_sample(ElectricitySample(T0 + timedelta(minutes=20), energy_kwh=0.05, price_per_kwh=PRICE))  # reboot
    acc.add_sample(ElectricitySample(T0 + timedelta(minutes=30), energy_kwh=0.15, price_per_kwh=PRICE))
    assert acc.meter_resets == 1
    assert acc.energy_kwh == pytest.approx(0.25)
    assert acc.reported_energy_kwh == pytest.approx(0.25)
    assert acc.cost == pytest.approx(Decimal("0.25") * PRICE, abs=Decimal("1e-9"))


def test_plug_counter_reset_with_power_never_goes_down():
    acc = ElectricityAccumulator()
    shown = []
    acc.add_sample(ElectricitySample(T0, power_w=600, energy_kwh=5.0, price_per_kwh=PRICE))
    shown.append(acc.reported_energy_kwh)
    acc.add_sample(ElectricitySample(T0 + timedelta(minutes=10), power_w=600, energy_kwh=5.1, price_per_kwh=PRICE))
    shown.append(acc.reported_energy_kwh)
    acc.add_sample(ElectricitySample(T0 + timedelta(minutes=20), power_w=600, energy_kwh=0.01, price_per_kwh=PRICE))
    shown.append(acc.reported_energy_kwh)
    assert shown == sorted(shown)
    assert acc.energy_kwh == pytest.approx(0.2)  # 0.1 from the meter, 0.1 estimated across the reboot


def test_restart_keeps_the_highest_value_shown():
    acc = ElectricityAccumulator()
    _plug_like_centauri(acc, 7)
    before = acc.reported_energy_kwh
    assert before > acc.energy_kwh  # an estimate is ahead of the meter right now
    restored = ElectricityAccumulator.from_dict(acc.as_dict())
    assert restored.reported_energy_kwh == before
    assert restored.reported_cost == acc.reported_cost
    # Next sample after the restart: still not lower.
    restored.add_sample(
        ElectricitySample(T0 + timedelta(minutes=9), power_w=0.0, energy_kwh=acc.last_energy_kwh, price_per_kwh=PRICE)
    )
    assert restored.reported_energy_kwh >= before


def test_counter_saved_before_0125_starts_from_its_total():
    acc = ElectricityAccumulator()
    _plug_like_centauri(acc, 7)
    old = acc.as_dict()
    del old["shown_energy_kwh"], old["shown_cost"]
    restored = ElectricityAccumulator.from_dict(old)
    assert restored.reported_energy_kwh == acc.total_energy_kwh
    assert restored.reported_cost == acc.total_cost


async def step(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: float, energy: str, power: str = "300"
) -> None:
    """Time passes; the plug reports (if it changed); the periodic update runs."""
    freezer.tick(timedelta(minutes=minutes))
    hass.states.async_set(POWER, power, {"unit_of_measurement": "W"})
    hass.states.async_set(ENERGY, energy, {"unit_of_measurement": "Wh"})
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


# --------------------------------------------------------------------------- #
# End to end (Bambu Lab printer with a plug: power + energy meter)
# --------------------------------------------------------------------------- #


async def test_sensor_does_not_go_down_when_the_meter_steps(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-11-02 12:10:00+00:00")
    set_states(hass, status="idle", energy="100000")
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await step(hass, freezer, 2, energy="100000")  # 300 W x 2 min: 10 Wh estimated
    estimated = float(hass.states.get("sensor.x2d_energy_total").state)
    assert estimated == pytest.approx(0.01)
    await step(hass, freezer, 0.01, energy="100008")  # the meter only counted 8 Wh
    assert float(hass.states.get("sensor.x2d_energy_total").state) >= estimated
    assert float(hass.states.get("sensor.x2d_energy_today").state) >= estimated
    await step(hass, freezer, 1, energy="100015")  # 15 Wh in all: it goes on from there
    assert float(hass.states.get("sensor.x2d_energy_total").state) == pytest.approx(0.015)


async def test_restored_value_is_the_floor_after_restart(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """HA stopped without its last save: the sensors' last values still win over the older file."""
    freezer.move_to("2026-11-02 12:24:00+00:00")
    now = dt_util.utcnow()
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State("sensor.x2d_energy_total", "0.212470", last_updated=now - timedelta(minutes=1)),
                {"native_value": 0.21247, "native_unit_of_measurement": "kWh"},
            ),
            (
                State("sensor.x2d_cost_total", "0.04", last_updated=now - timedelta(minutes=1)),
                {
                    "native_value": {"__type": "<class 'decimal.Decimal'>", "decimal_str": "0.04"},
                    "native_unit_of_measurement": "EUR",
                },
            ),
            (
                State("sensor.x2d_energy_today", "0.141", last_updated=now - timedelta(minutes=1)),
                {"native_value": 0.141, "native_unit_of_measurement": "kWh"},
            ),
            (
                # Yesterday's value: the day counter must not restart from it.
                State("sensor.x2d_cost_today", "0.5", last_updated=now - timedelta(days=1)),
                {"native_value": 0.5, "native_unit_of_measurement": "EUR"},
            ),
        ],
    )
    set_states(hass, status="idle", energy="100000", power="0")
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert float(hass.states.get("sensor.x2d_energy_total").state) == pytest.approx(0.21247)
    assert float(hass.states.get("sensor.x2d_cost_total").state) == pytest.approx(0.04)
    assert float(hass.states.get("sensor.x2d_energy_today").state) == pytest.approx(0.141)
    assert float(hass.states.get("sensor.x2d_cost_today").state) < 0.5

    # It keeps counting from there once the meter has counted past it.
    await step(hass, freezer, 1, energy="100000", power="0")
    assert float(hass.states.get("sensor.x2d_energy_total").state) == pytest.approx(0.21247)
    await step(hass, freezer, 60, energy="100300", power="0")
    assert float(hass.states.get("sensor.x2d_energy_total").state) == pytest.approx(0.3)


async def test_midnight_resets_today_only(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await hass.config.async_set_time_zone("Europe/Lisbon")
    freezer.move_to("2026-11-02 23:00:00+00:00")
    set_states(hass, status="idle", energy="100000", power="0")
    entry = make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await step(hass, freezer, 30, energy="100100", power="0")
    assert float(hass.states.get("sensor.x2d_energy_today").state) == pytest.approx(0.1)
    await step(hass, freezer, 40, energy="100150", power="0")  # 00:10: a new day
    await step(hass, freezer, 10, energy="100200", power="0")
    assert float(hass.states.get("sensor.x2d_energy_today").state) == pytest.approx(0.05)
    assert float(hass.states.get("sensor.x2d_energy_total").state) == pytest.approx(0.2)


# --------------------------------------------------------------------------- #
# Moonraker POST: the "json" parameter hid the json module (every POST raised)
# --------------------------------------------------------------------------- #


class _Content:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, _size: int):
        if self._body:
            yield self._body


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.content_length = len(body)
        self.content = _Content(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class _Session:
    """The bits of aiohttp.ClientSession the client uses; records each POST."""

    def __init__(self, status: int = 200, body: bytes = b'{"result": "ok"}') -> None:
        self.status, self.body = status, body
        self.posts: list[tuple[str, Any]] = []

    def post(self, url: str, *, headers=None, json=None, timeout=None) -> _Response:
        self.posts.append((url, json))
        return _Response(self.status, self.body)


def _client(session: _Session) -> MoonrakerClient:
    return MoonrakerClient(session, "192.168.1.89", 7125)  # type: ignore[arg-type]


async def test_post_reads_the_reply() -> None:
    session = _Session(body=b'{"result": "ok"}')
    assert await _client(session)._post("/printer/print/pause") == {"result": "ok"}
    assert session.posts == [("http://192.168.1.89:7125/printer/print/pause", None)]


async def test_post_with_an_empty_reply_gives_an_empty_dict() -> None:
    assert await _client(_Session(body=b""))._post("/machine/shutdown") == {}
    assert await _client(_Session(body=b"  \n"))._post("/machine/shutdown") == {}


async def test_post_sends_the_json_body() -> None:
    session = _Session(body=b'{"result": {"id": 7}}')
    assert await _client(session)._post("/server/spoolman/proxy", {"a": 1}) == {"result": {"id": 7}}
    assert session.posts == [("http://192.168.1.89:7125/server/spoolman/proxy", {"a": 1})]


async def test_gcode_and_controls_work_end_to_end() -> None:
    session = _Session()
    client = _client(session)
    await client.gcode("SET_PIN PIN=x VALUE=1")
    await client.pause()
    await client.resume()
    await client.cancel()
    await client.shutdown_host()
    urls = [url for url, _ in session.posts]
    assert urls[0] == "http://192.168.1.89:7125/printer/gcode/script?script=SET_PIN%20PIN%3Dx%20VALUE%3D1"
    assert urls[1:] == [
        "http://192.168.1.89:7125/printer/print/pause",
        "http://192.168.1.89:7125/printer/print/resume",
        "http://192.168.1.89:7125/printer/print/cancel",
        "http://192.168.1.89:7125/machine/shutdown",
    ]


async def test_post_error_keeps_moonrakers_reason() -> None:
    session = _Session(status=500, body=b"Error running shell command")
    with pytest.raises(MoonrakerConnectionError, match="Error running shell command"):
        await _client(session).shutdown_host()


def test_no_parameter_or_variable_hides_the_json_module() -> None:
    package = Path(__file__).parent.parent / "custom_components" / "fdm_printer_manager"
    offenders = []
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            shadow_arg = isinstance(node, ast.arg) and node.arg == "json"
            shadow_var = isinstance(node, ast.Name) and node.id == "json" and isinstance(node.ctx, ast.Store)
            if shadow_arg or shadow_var:
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []
