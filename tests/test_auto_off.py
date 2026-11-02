"""Auto-off after a print, the "is the computer off yet?" check, and the SSH fallback (0.8.0).

0.12.3: no 15-minute timer any more: the safe power off starts as soon as a print ends well and
waits for the nozzle to drop below 80 °C.
"""

from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
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
        self.answers: list[bool] = []  # next host_is_up answers, before falling back to computer_on

    async def status(self) -> KlipperStatus:
        if isinstance(self.reading, Exception):
            raise self.reading
        return self.reading

    async def shutdown(self) -> None:
        self.calls.append("shutdown")
        if self.shutdown_error is not None:
            raise self.shutdown_error

    async def host_is_up(self, host: str, port: int) -> bool:
        if self.answers:
            return self.answers.pop(0)
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


async def finish_a_print(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine, end: str, nozzle: float = 60.0
) -> None:
    machine.reading = KlipperStatus(state="printing", filename="a.gcode", progress=50, nozzle_temp=210.0)
    await tick(hass, freezer, 5)
    machine.reading = KlipperStatus(state=end, filename="a.gcode", progress=100, nozzle_temp=nozzle)
    await tick(hass, freezer, 5)


def attrs(hass: HomeAssistant) -> dict:
    return hass.states.get(SAFE).attributes


async def computer_goes_off(hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine) -> None:
    machine.reading = MoonrakerConnectionError("host is down")
    machine.computer_on = False
    for _ in range(6):  # 3 checks without an answer (15 s) + 10 s more
        await tick(hass, freezer, 5)


async def test_turns_off_as_soon_as_a_successful_print_ends(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    """0.12.3: no 15-minute wait. The nozzle is already below 80 °C: the computer is shut down at once."""
    freezer.move_to("2026-11-01 10:00:00+00:00")
    await setup(hass)
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")
    await finish_a_print(hass, freezer, machine, "complete")
    assert machine.calls == ["shutdown"]
    assert phase(hass) == "shutting_down"
    assert attrs(hass)["auto_off"] is True
    assert "auto_off_at" not in attrs(hass)
    assert "auto_off_minutes" not in attrs(hass)
    assert cut == []

    await computer_goes_off(hass, freezer, machine)
    assert len(cut) == 1
    assert cut[0].data["entity_id"] == PLUG
    assert attrs(hass)["last_result"] == "power_off"


async def test_the_plug_is_not_cut_while_the_computer_still_answers(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    await setup(hass)
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")
    await finish_a_print(hass, freezer, machine, "complete")
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
    await setup(hass)
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")
    await finish_a_print(hass, freezer, machine, "complete")
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


async def test_auto_off_switched_off(hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    await setup(hass, auto_off=False)
    await finish_a_print(hass, freezer, machine, "complete")
    assert phase(hass) == "on"
    assert attrs(hass)["auto_off"] is False
    await tick(hass, freezer, 3600)
    assert machine.calls == []


async def test_a_new_print_cancels_the_auto_off(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    await setup(hass)
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")
    await finish_a_print(hass, freezer, machine, "complete", nozzle=200.0)
    assert phase(hass) == "cooling"  # started at once, waiting for the nozzle
    machine.reading = KlipperStatus(state="printing", filename="b.gcode", progress=1, nozzle_temp=200.0)
    await tick(hass, freezer, 5)
    assert phase(hass) == "on"
    assert attrs(hass)["last_result"] == "aborted"
    machine.reading = KlipperStatus(state="standby", nozzle_temp=30.0)  # cool again: still cancelled
    await tick(hass, freezer, 3600)
    assert machine.calls == []
    assert cut == []


async def test_turning_on_cancels_the_auto_off(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    await setup(hass)
    async_mock_service(hass, "homeassistant", "turn_on")
    await finish_a_print(hass, freezer, machine, "complete", nozzle=200.0)
    assert phase(hass) == "cooling"
    await hass.services.async_call("switch", "turn_on", {"entity_id": SAFE}, blocking=True)
    await hass.async_block_till_done()
    assert phase(hass) == "on"
    machine.reading = KlipperStatus(state="complete", nozzle_temp=30.0)
    await tick(hass, freezer, 3600)
    assert machine.calls == []


async def test_the_plug_turned_off_by_hand_ends_the_auto_off(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    await setup(hass)
    await finish_a_print(hass, freezer, machine, "complete", nozzle=200.0)
    assert phase(hass) == "cooling"
    hass.states.async_set(PLUG, "off")
    await tick(hass, freezer, 5)
    assert phase(hass) == "off"
    assert attrs(hass)["last_result"] == "aborted"
    hass.states.async_set(PLUG, "on")
    machine.reading = KlipperStatus(state="complete", nozzle_temp=30.0)
    await tick(hass, freezer, 3600)
    assert machine.calls == []


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
    """Starts at once and waits for the nozzle (up to 30 min), never on a timer."""
    freezer.move_to("2026-11-01 10:00:00+00:00")
    await setup(hass)
    await finish_a_print(hass, freezer, machine, "complete", nozzle=200.0)
    assert phase(hass) == "cooling"
    machine.reading = KlipperStatus(state="complete", nozzle_temp=120.0)
    for _ in range(20 * 12):  # 20 minutes still hot: no shutdown, still cooling
        await tick(hass, freezer, 5)
    assert phase(hass) == "cooling"
    assert machine.calls == []
    machine.reading = KlipperStatus(state="complete", nozzle_temp=79.0)
    await tick(hass, freezer, 5)
    assert machine.calls == ["shutdown"]
    assert phase(hass) == "shutting_down"


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
    machine.ssh.assert_awaited_once_with("192.168.1.89", COSMOS_LOGIN, host_key=None)
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
    machine.ssh.assert_awaited_once_with("192.168.1.89", COSMOS_LOGIN, host_key=None)


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


# --------------------------------------------------------------------------- #
# Moonraker not answering (0.12.2): no contact, no cut, no SSH (was: SSH, or cut if the computer was off)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("computer_on", [True, False])
async def test_moonraker_down_never_cuts_and_never_sends_ssh(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine, computer_on: bool
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    machine.reading = MoonrakerConnectionError("Moonraker crashed")
    machine.computer_on = computer_on
    await setup(hass, ssh_username="pi", ssh_password="secret")
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")
    with pytest.raises(HomeAssistantError, match="no contact with the printer"):
        await hass.services.async_call("switch", "turn_off", {"entity_id": SAFE}, blocking=True)
    assert cut == []
    assert machine.calls == []
    machine.ssh.assert_not_awaited()
    assert attrs(hass)["last_result"] == "no_contact"


# --------------------------------------------------------------------------- #
# Auto-off only after a successful print, and only while the printer answers (0.12.2)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("end", ["cancelled", "error", "standby"])
async def test_no_auto_off_after_a_failed_cancelled_or_unknown_print(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine, end: str
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    await setup(hass)
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")
    await finish_a_print(hass, freezer, machine, end)
    assert phase(hass) == "on"
    await tick(hass, freezer, 3600)
    assert machine.calls == []
    assert cut == []


async def test_print_interrupted_by_lost_contact_gets_no_auto_off(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    """The printer stops answering mid-print: the watchdog closes it as failed, nothing is cut."""
    freezer.move_to("2026-11-01 10:00:00+00:00")
    await setup(hass)
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")
    machine.reading = KlipperStatus(state="printing", filename="a.gcode", progress=50, nozzle_temp=210.0)
    await tick(hass, freezer, 5)
    machine.reading = MoonrakerConnectionError("gone")
    for _ in range(30):
        await tick(hass, freezer, 60)
    assert phase(hass) == "on"
    assert machine.calls == []
    assert cut == []


async def test_losing_contact_while_cooling_after_a_print_cancels_the_auto_off(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    await setup(hass)
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")
    await finish_a_print(hass, freezer, machine, "complete", nozzle=200.0)
    assert phase(hass) == "cooling"
    machine.reading = MoonrakerConnectionError("gone")
    await tick(hass, freezer, 5)
    await tick(hass, freezer, 5)
    assert phase(hass) == "on"
    assert attrs(hass)["last_result"] == "no_contact"
    machine.reading = KlipperStatus(state="complete", nozzle_temp=30.0)  # back: the auto-off stays cancelled
    await tick(hass, freezer, 3600)
    assert machine.calls == []
    assert cut == []


async def test_losing_contact_while_cooling_leaves_the_plug_on(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    """Before 0.12.2 no temperature reading counted as cool and the plug was cut."""
    freezer.move_to("2026-11-01 10:00:00+00:00")
    machine.reading = KlipperStatus(state="complete", nozzle_temp=150.0)
    await setup(hass)
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")
    with patch(f"{COORD}.persistent_notification.async_create") as notify:
        await hass.services.async_call("switch", "turn_off", {"entity_id": SAFE}, blocking=True)
        assert phase(hass) == "cooling"
        machine.reading = MoonrakerConnectionError("gone")
        for _ in range(4):
            await tick(hass, freezer, 5)
    assert cut == []
    assert machine.calls == []
    assert phase(hass) == "on"
    assert attrs(hass)["last_result"] == "no_contact"
    assert "power not cut" in notify.call_args.args[1]


async def test_power_off_refused_while_a_print_is_still_open(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    await setup(hass)
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")
    machine.reading = KlipperStatus(state="printing", filename="a.gcode", progress=50, nozzle_temp=30.0)
    await tick(hass, freezer, 5)
    machine.reading = KlipperStatus(state="startup", filename="a.gcode", nozzle_temp=30.0)  # unknown: job stays open
    await tick(hass, freezer, 5)
    with pytest.raises(HomeAssistantError, match="a print is still open"):
        await hass.services.async_call("switch", "turn_off", {"entity_id": SAFE}, blocking=True)
    assert cut == []
    assert machine.calls == []


# --------------------------------------------------------------------------- #
# SSH host key: trusted on first use (0.12.2)
# --------------------------------------------------------------------------- #
async def test_ssh_host_key_is_remembered_and_then_required(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    machine.shutdown_error = MoonrakerConnectionError(CENTAURI_SHUTDOWN_500)
    machine.ssh.return_value = "ssh-ed25519 AAAAC3first"
    entry = await setup(hass)
    async_mock_service(hass, "homeassistant", "turn_off")
    await hass.services.async_call("switch", "turn_off", {"entity_id": SAFE}, blocking=True)
    assert machine.ssh.await_args.kwargs == {"host_key": None}
    assert entry.runtime_data.identity.known["ssh_host_key"] == "ssh-ed25519 AAAAC3first"
    await computer_goes_off(hass, freezer, machine)

    machine.reading = KlipperStatus(state="standby", nozzle_temp=30.0)
    await tick(hass, freezer, 5)
    await hass.services.async_call("switch", "turn_off", {"entity_id": SAFE}, blocking=True)
    assert machine.ssh.await_args.kwargs == {"host_key": "ssh-ed25519 AAAAC3first"}


async def test_changed_ssh_host_key_blocks_the_power_off(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine
) -> None:
    from custom_components.fdm_printer_manager.adapters.ssh import SshHostKeyChanged  # noqa: PLC0415

    freezer.move_to("2026-11-01 10:00:00+00:00")
    machine.shutdown_error = MoonrakerConnectionError(CENTAURI_SHUTDOWN_500)
    machine.ssh.side_effect = SshHostKeyChanged("the printer's SSH host key changed")
    entry = await setup(hass)
    entry.runtime_data.identity.known["ssh_host_key"] = "ssh-ed25519 AAAAC3first"
    cut: list[ServiceCall] = async_mock_service(hass, "homeassistant", "turn_off")
    await hass.services.async_call("switch", "turn_off", {"entity_id": SAFE}, blocking=True)
    assert cut == []
    assert attrs(hass)["last_result"] == "identity"
    assert attrs(hass)["identity_ok"] is False
    issues = ir.async_get(hass).issues
    assert (DOMAIN, f"identity_changed_{entry.entry_id}") in issues
    await computer_goes_off(hass, freezer, machine)
    assert cut == []


# --------------------------------------------------------------------------- #
# 0.12.3: auto_off_minutes (0 = never) becomes the on/off auto_off
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("minutes", "expected"), [(15, True), (1, True), (0, False)])
async def test_old_minutes_are_migrated_to_on_off(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, machine: Machine, minutes: int, expected: bool
) -> None:
    freezer.move_to("2026-11-01 10:00:00+00:00")
    entry = await setup(hass, auto_off_minutes=minutes, update_interval=30)
    assert entry.minor_version == 2
    assert entry.options == {"auto_off": expected, "update_interval": 30}
    assert attrs(hass)["auto_off"] is expected
    await finish_a_print(hass, freezer, machine, "complete")
    assert machine.calls == (["shutdown"] if expected else [])


async def test_migration_keeps_entries_without_the_old_option(hass: HomeAssistant, machine: Machine) -> None:
    entry = await setup(hass)
    assert entry.minor_version == 2
    assert "auto_off" not in entry.options  # the default applies: on
    assert attrs(hass)["auto_off"] is True


def test_migrate_auto_off_values() -> None:
    from custom_components.fdm_printer_manager import migrate_auto_off  # noqa: PLC0415

    assert migrate_auto_off({"auto_off_minutes": 15, "x": 1}) == {"auto_off": True, "x": 1}
    assert migrate_auto_off({"auto_off_minutes": 0}) == {"auto_off": False}
    assert migrate_auto_off({"auto_off_minutes": 0.0}) == {"auto_off": False}
    assert migrate_auto_off({"auto_off_minutes": None}) == {"auto_off": False}
    assert migrate_auto_off({"auto_off_minutes": 30, "auto_off": False}) == {"auto_off": False}  # new value wins
    assert migrate_auto_off({"x": 1}) == {"x": 1}
