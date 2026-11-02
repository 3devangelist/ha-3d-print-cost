"""Shared fixtures."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/ in every test."""
    return


@pytest.fixture(autouse=True)
def no_network_scan():
    """No background Klipper scan of the real network when the integration starts (test_scanner.py tests it)."""
    with patch("custom_components.fdm_printer_manager.async_start_scanning", return_value=lambda: None):
        yield


@pytest.fixture(autouse=True)
def no_real_power_off():
    """No ping, TCP check or SSH to real printers (the computer counts as off; SSH fails)."""
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from custom_components.fdm_printer_manager.adapters.ssh import SshError  # noqa: PLC0415

    with (
        patch("custom_components.fdm_printer_manager.coordinator.host_is_up", AsyncMock(return_value=False)),
        patch(
            "custom_components.fdm_printer_manager.coordinator.ssh_shutdown",
            AsyncMock(side_effect=SshError("no SSH in tests")),
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def no_klipper_extras():
    """Klipper printers in tests have no lights, baby steps or filament unit unless a test says so."""
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from custom_components.fdm_printer_manager.adapters.klipper_extras import KlipperFeatures  # noqa: PLC0415

    with patch(
        "custom_components.fdm_printer_manager.adapters.moonraker.MoonrakerClient.features",
        AsyncMock(return_value=KlipperFeatures()),
    ):
        yield


@pytest.fixture(autouse=True)
def no_machine_identity():
    """Klipper printers in tests do not report a machine identity unless a test says so (identity.py)."""
    from unittest.mock import AsyncMock  # noqa: PLC0415

    with patch(
        "custom_components.fdm_printer_manager.adapters.moonraker.MoonrakerClient.machine_identity",
        AsyncMock(return_value=None),
    ):
        yield


@pytest.fixture
def panel_entry(hass):
    """The sidebar page entry already exists, so "Add integration" shows the printer types."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: PLC0415

    entry = MockConfigEntry(domain="fdm_printer_manager", unique_id="panel", title="FDM Printers", data={"panel": True})
    entry.add_to_hass(hass)
    return entry
