"""Firmware from Moonraker's system_info; Cosmos before 26.10 gets an update recommendation (0.12.3)."""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from custom_components.fdm_printer_manager import own_device
from custom_components.fdm_printer_manager.const import DOMAIN
from custom_components.fdm_printer_manager.firmware import (
    COSMOS_RELEASES_URL,
    Firmware,
    firmware_from_system_info,
    older_than,
    parse_version,
)

from .test_auto_off import Machine, machine, setup  # noqa: F401 - machine is a fixture

STATUS = "sensor.ender_6_printer_status"


def cosmos(version: str, name: str | None = None) -> dict:
    """``machine.system_info`` as OpenCentauri Cosmos reports it (26.10.1 on 02/11/2026)."""
    return {
        "distribution": {
            "id": "cosmos",
            "version": version,
            "name": name or f"OpenCentauri Cosmos {version} (wrynose)",
        }
    }


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("26.10.1", (26, 10, 1)),
        ("26.10", (26, 10)),
        ("26.09.0", (26, 9, 0)),
        ("0.0.7", (0, 0, 7)),
        ("v26.11-rc1", (26, 11)),
        ("", None),
        (None, None),
        ("wrynose", None),
    ],
)
def test_parse_version(text, parsed) -> None:
    assert parse_version(text) == parsed


@pytest.mark.parametrize(
    ("version", "older"),
    [
        ((26, 9, 0), True),
        ((0, 0, 7), True),
        ((25, 12), True),
        ((26, 10), False),
        ((26, 10, 0), False),
        ((26, 10, 1), False),
        ((27, 1), False),
    ],
)
def test_older_than_26_10(version, older) -> None:
    assert older_than(version, (26, 10)) is older


def test_firmware_from_system_info() -> None:
    fw = firmware_from_system_info(cosmos("26.10.1"))
    assert fw == Firmware("cosmos", "26.10.1", "OpenCentauri Cosmos 26.10.1 (wrynose)")
    assert fw.label == "OpenCentauri Cosmos 26.10.1 (wrynose)"
    assert not fw.update_recommended
    assert firmware_from_system_info(cosmos("26.09.0")).update_recommended
    assert firmware_from_system_info(cosmos("0.0.7")).update_recommended
    assert not firmware_from_system_info(cosmos("unknown")).update_recommended  # no verdict
    # Other distributions are never flagged, whatever their version
    debian = {"distribution": {"id": "debian", "version": "12", "name": "Debian GNU/Linux 12 (bookworm)"}}
    assert not firmware_from_system_info(debian).update_recommended
    assert firmware_from_system_info({"distribution": {"id": "", "version": "1"}}) is None
    assert firmware_from_system_info({}) is None
    assert firmware_from_system_info(None) is None


def _issue(hass: HomeAssistant, entry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"firmware_update_{entry.entry_id}")


@pytest.mark.parametrize("version", ["26.09.0", "0.0.7"])
async def test_old_cosmos_gets_a_recommendation_cleared_after_the_update(
    hass: HomeAssistant,
    machine: Machine,  # noqa: F811
    version: str,
) -> None:
    entry = await setup(hass)
    coordinator = entry.runtime_data
    coordinator.klipper.client.system_info = cosmos(version)
    await coordinator.async_check_klipper_identity()

    issue = _issue(hass, entry)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.is_fixable is False
    assert issue.learn_more_url == COSMOS_RELEASES_URL
    assert issue.translation_placeholders["version"] == version
    attrs = hass.states.get(STATUS).attributes
    assert attrs["firmware_update"] is True
    assert attrs["firmware"] == f"OpenCentauri Cosmos {version} (wrynose)"
    device = own_device(dr.async_get(hass), entry.entry_id)
    assert device.sw_version == f"OpenCentauri Cosmos {version} (wrynose)"

    # Updated to 26.10.1: the recommendation goes away
    coordinator.klipper.client.system_info = cosmos("26.10.1")
    await coordinator.async_check_klipper_identity()
    assert _issue(hass, entry) is None
    assert hass.states.get(STATUS).attributes["firmware_update"] is False
    device = own_device(dr.async_get(hass), entry.entry_id)
    assert device.sw_version == "OpenCentauri Cosmos 26.10.1 (wrynose)"


async def test_new_cosmos_and_other_firmware_get_no_recommendation(
    hass: HomeAssistant,
    machine: Machine,  # noqa: F811
) -> None:
    entry = await setup(hass)
    coordinator = entry.runtime_data
    coordinator.async_firmware_seen(cosmos("26.10.1"))
    assert _issue(hass, entry) is None
    coordinator.async_firmware_seen(
        {"distribution": {"id": "debian", "version": "11", "name": "Debian GNU/Linux 11 (bullseye)"}}
    )
    assert _issue(hass, entry) is None
    assert hass.states.get(STATUS).attributes["firmware"] == "Debian GNU/Linux 11 (bullseye)"
    assert hass.states.get(STATUS).attributes["firmware_update"] is False


async def test_no_distribution_changes_nothing(hass: HomeAssistant, machine: Machine) -> None:  # noqa: F811
    entry = await setup(hass)
    coordinator = entry.runtime_data
    coordinator.async_firmware_seen(cosmos("26.09.0"))
    assert _issue(hass, entry) is not None
    coordinator.async_firmware_seen({"cpu_info": {}})  # Moonraker did not say: the last verdict stays
    assert _issue(hass, entry) is not None
    assert coordinator.firmware.version == "26.09.0"
