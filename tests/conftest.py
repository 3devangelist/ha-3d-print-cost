"""Shared fixtures."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/ in every test."""
    return


@pytest.fixture
def panel_entry(hass):
    """The sidebar page entry already exists, so "Add integration" shows the printer types."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: PLC0415

    entry = MockConfigEntry(domain="fdm_printer_manager", unique_id="panel", title="FDM Printers", data={"panel": True})
    entry.add_to_hass(hass)
    return entry
