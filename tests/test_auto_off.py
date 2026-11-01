"""Auto-off after a print, the "is the computer off yet?" check, and the SSH fallback (0.8.0)."""

from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.fdm_printer_manager.adapters.moonraker import KlipperStatus, MoonrakerConnectionError
from custom_components.fdm_printer_manager.adapters.ssh import COSMOS_LOGIN, SshLogin
from custom_components.fdm_printer_manager.const import DOMAIN

from .test_controls import CLIENT, ENERGY, PLUG, phase, tick

COORD = "custom_components.fdm_printer_manager.coordinator"
SAFE = "switch.ender_6_safe_power"

# What the Elegoo Centauri Carbon really answers to POST /machine/shutdown (01/11/2026, firmware "cosmos")
CENTAURI_SHUTDOWN_500 = (
    "HTTP 500 from http://192.168.1.95:80/machine/shutdown: "
    '{"error": {"code": 500, "message": "Error running shell command: \'sudo systemctl poweroff\'", '
    '"traceback": "ShellCommandError"}}'
)


class Machine:
    """A Klipper printer whose computer can be on or off independently of Moonraker."""

    def __init__(self) -> None:
        self.reading: KlipperStatus | Exception = KlipperStatus(state="standby", nozzle_temp=30.0)
        self.computer_on = True
        self.calls: list[str] = []
        self.shutdown_error: Exception | None = None
        self.name = "Ender6"

    async def status(self) -> KlipperStatus:
        if isinstance(self.reading, Exception):
            raise self.reading
        return self.reading

    async def shutdown(self) -> None:
        self.calls.append("shutdown")
        if self.shutdown_error is not None:
            raise self.shutdown_error

    async def host_is_up(self, host: str, port: int) -> bool:
        return self.computer_on

    async def hostname(self) -> str:
        return self.name


@pytest.fixture
def machine():
    fake = Machine()
    ssh = AsyncMock()
    with (
        patch.multiple(
            CLIENT,
            status=AsyncMock(side_effect=fake.status),
            metadata=AsyncMock(side_effect=MoonrakerConnectionError("none")),
            shutdown_host=AsyncMock(side_effect=fake.shutdown),
            webcams=AsyncMock(return_value=[]),
            hostname=AsyncMock(side_effect=fake.hostname),
        ),
        patch(f"{COORD}.host_is_up", AsyncMock(side_effect=fake.host_is_up)),
        patch(f"{COORD}.ssh_shutdown", ssh),
    ):
        fake.ssh = ssh
        yield fake


async def setup(hass: HomeAssistant, **options) -> MockConfigEntry:
    hass.states.async_set(PLUG, "on")
    hass.states.async_set(ENERGY, "10000", {"unit_of_measurement": "Wh"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Ender 6",
        unique_id="klipper_192.168.1.89_7125",
        data={
            "printer_type": "klipper",
            "printer_name": "Ender 6",
            "host": "192.168.1.89",
            "port": 7125,
            "switch_entity": PLUG,
            "energy_entity": ENERGY,
            "price_per_kwh": 0.16,
        },
        options=options,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def finish_a_print(hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine, end: str) -> None:
    machine.reading = KlipperStatus(state="printing", filename="a.gcode", progress=50, nozzle_temp=210.0)
    await tick(hass, freezer, 5)
    machine.reading = KlipperStatus(state=end, filename="a.gcode", progress=100, nozzle_temp=60.0)
    await tick(hass, freezer, 5)


def attrs(hass: HomeAssistant) -> dict:
    return hass.states.get(SAFE).attributes


async def computer_goes_off(hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine) -> None:
    machine.reading = MoonrakerConnectionError("host is down")
    machine.computer_on = False
    for _ in range(6):  # 3 checks without an answer (15 s) + 10 s more
        await tick(hass, freezer, 5)


@pytest.mark.parametrize("end", ["complete", "cancelled", "error"])
async def test_turns_off_15_minutes_after_any_end(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine, end: str
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    await setup(hass)
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")
    await finish_a_print(hass, freezer, machine, end)
    assert phase(hass) == "waiting"
    assert attrs(hass)["auto_off_at"].startswith("2026-11-01T10:15:10")
    assert attrs(hass)["auto_off_minutes"] == 15

    await tick(hass, freezer, 14 * 60)
    assert machine.calls == []
    await tick(hass, freezer, 60 + 5)
    assert machine.calls == ["shutdown"]
    assert phase(hass) == "shutting_down"
    assert cut == []

    await computer_goes_off(hass, freezer, machine)
    assert len(cut) == 1
    assert cut[0].data["entity_id"] == PLUG
    assert attrs(hass)["last_result"] == "power_off"


async def test_the_plug_is_not_cut_while_the_computer_still_answers(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    await setup(hass, auto_off_minutes=1)
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")
    await finish_a_print(hass, freezer, machine, "complete")
    await tick(hass, freezer, 65)
    assert machine.calls == ["shutdown"]

    machine.reading = MoonrakerConnectionError("Moonraker stopped")  # Moonraker gone, the computer still up
    for _ in range(12):
        await tick(hass, freezer, 5)
    assert cut == []  # Moonraker alone going quiet is not enough

    await tick(hass, freezer, 5 * 60)
    assert cut == []
    assert attrs(hass)["last_result"] == "still_on"
    assert phase(hass) == "on"


async def test_a_short_dropout_does_not_count(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    await setup(hass, auto_off_minutes=1)
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")
    await finish_a_print(hass, freezer, machine, "complete")
    await tick(hass, freezer, 65)
    machine.computer_on = False
    await tick(hass, freezer, 5)
    await tick(hass, freezer, 5)
    machine.computer_on = True  # two missed answers, then it answers again
    await tick(hass, freezer, 5)
    machine.computer_on = False
    await tick(hass, freezer, 5)
    await tick(hass, freezer, 5)
    assert cut == []
    await tick(hass, freezer, 5)  # third in a row
    await tick(hass, freezer, 10)
    assert len(cut) == 1


async def test_zero_minutes_turns_auto_off_off(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    await setup(hass, auto_off_minutes=0)
    await finish_a_print(hass, freezer, machine, "complete")
    assert phase(hass) == "on"
    await tick(hass, freezer, 3600)
    assert machine.calls == []


async def test_a_new_print_cancels_the_auto_off(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    await setup(hass)
    await finish_a_print(hass, freezer, machine, "complete")
    assert phase(hass) == "waiting"
    machine.reading = KlipperStatus(state="printing", filename="b.gcode", progress=1, nozzle_temp=200.0)
    await tick(hass, freezer, 5)
    await tick(hass, freezer, 5)
    assert phase(hass) == "on"
    assert attrs(hass)["last_result"] == "aborted"
    await tick(hass, freezer, 3600)
    assert machine.calls == []


async def test_turning_on_cancels_the_auto_off(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    await setup(hass)
    async_mock_service(hass, "homeassistant", "turn_on")
    await finish_a_print(hass, freezer, machine, "complete")
    assert phase(hass) == "waiting"
    await hass.services.async_call("switch", "turn_on", {"entity_id": SAFE}, blocking=True)
    await hass.async_block_till_done()
    assert phase(hass) == "on"
    await tick(hass, freezer, 3600)
    assert machine.calls == []


async def test_the_plug_turned_off_by_hand_ends_the_auto_off(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    await setup(hass)
    await finish_a_print(hass, freezer, machine, "complete")
    hass.states.async_set(PLUG, "off")
    await tick(hass, freezer, 5)
    await tick(hass, freezer, 3600)
    assert machine.calls == []
    assert attrs(hass)["last_result"] == "aborted"


async def test_no_auto_off_when_the_plug_is_already_off(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    await setup(hass)
    hass.states.async_set(PLUG, "off")
    await finish_a_print(hass, freezer, machine, "complete")
    assert phase(hass) == "off"


async def test_auto_off_waits_for_the_nozzle_to_cool(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    await setup(hass, auto_off_minutes=1)
    await finish_a_print(hass, freezer, machine, "complete")
    machine.reading = KlipperStatus(state="complete", nozzle_temp=120.0)  # reheated meanwhile
    await tick(hass, freezer, 65)
    assert phase(hass) == "cooling"
    assert machine.calls == []
    machine.reading = KlipperStatus(state="complete", nozzle_temp=70.0)
    await tick(hass, freezer, 5)
    assert machine.calls == ["shutdown"]


# --------------------------------------------------------------------------- #
# SSH fallback
# --------------------------------------------------------------------------- #
async def test_centauri_is_shut_down_over_ssh_without_setup(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    """Moonraker answers 500 "Error running shell command": root over SSH, no password, poweroff."""
    freezer.move_to("2026-11-01 10:00:00+00:00")
    machine.shutdown_error = MoonrakerConnectionError(CENTAURI_SHUTDOWN_500)
    machine.name = "unknown"
    await setup(hass)
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")
    await hass.services.async_call("switch", "turn_off", {"entity_id": SAFE}, blocking=True)
    assert machine.calls == ["shutdown"]
    machine.ssh.assert_awaited_once_with("192.168.1.89", COSMOS_LOGIN)
    assert COSMOS_LOGIN.username == "root"
    assert COSMOS_LOGIN.password is None
    assert COSMOS_LOGIN.command == "poweroff"
    assert phase(hass) == "shutting_down"

    await computer_goes_off(hass, freezer, machine)
    assert len(cut) == 1


async def test_cosmos_host_name_is_recognised(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    machine.shutdown_error = MoonrakerConnectionError("HTTP 404 from http://192.168.1.89:7125/machine/shutdown")
    machine.name = "cosmos"
    await setup(hass)
    await hass.services.async_call("switch", "turn_off", {"entity_id": SAFE}, blocking=True)
    machine.ssh.assert_awaited_once_with("192.168.1.89", COSMOS_LOGIN)


async def test_no_shutdown_possible_leaves_the_plug_on(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    machine.shutdown_error = MoonrakerConnectionError("HTTP 404 from http://192.168.1.89:7125/machine/shutdown")
    await setup(hass)
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")
    await hass.services.async_call("switch", "turn_off", {"entity_id": SAFE}, blocking=True)
    machine.ssh.assert_not_awaited()
    assert cut == []
    assert phase(hass) == "on"
    assert attrs(hass)["last_result"] == "no_shutdown"


async def test_ssh_login_from_the_options(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    machine.shutdown_error = MoonrakerConnectionError("HTTP 404 from http://192.168.1.89:7125/machine/shutdown")
    await setup(hass, ssh_username="pi", ssh_password="secret", ssh_command="sudo poweroff")
    assert attrs(hass)["ssh"] is True
    await hass.services.async_call("switch", "turn_off", {"entity_id": SAFE}, blocking=True)
    login: SshLogin = machine.ssh.await_args.args[1]
    assert (login.username, login.password, login.command) == ("pi", "secret", "sudo poweroff")
    assert "secret" not in repr(login)
    assert phase(hass) == "shutting_down"


async def test_moonraker_working_means_no_ssh(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    await setup(hass, ssh_username="pi", ssh_password="secret")
    await hass.services.async_call("switch", "turn_off", {"entity_id": SAFE}, blocking=True)
    assert machine.calls == ["shutdown"]
    machine.ssh.assert_not_awaited()
