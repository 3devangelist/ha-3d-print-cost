"""0.12.4: panel version from the manifest, hub diagnostics, no deprecated device registry call."""

import json
import re
from pathlib import Path

from homeassistant import config_entries
from homeassistant.components.frontend import DATA_PANELS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.fdm_printer_manager.const import DOMAIN, PANEL_URL_PATH
from custom_components.fdm_printer_manager.diagnostics import async_get_config_entry_diagnostics

from .test_integration import SERIAL, make_entry, set_states

PACKAGE = Path(__file__).parent.parent / "custom_components" / DOMAIN
MANIFEST_VERSION = json.loads((PACKAGE / "manifest.json").read_text(encoding="utf-8"))["version"]


async def _add_hub(hass: HomeAssistant) -> config_entries.ConfigEntry:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return hass.config_entries.async_entries(DOMAIN)[0]


async def test_panel_gets_the_manifest_version(hass: HomeAssistant) -> None:
    await _add_hub(hass)
    panel = hass.data[DATA_PANELS][PANEL_URL_PATH]
    assert panel.config["version"] == MANIFEST_VERSION
    assert panel.config["_panel_custom"]["module_url"].endswith(f"?v={MANIFEST_VERSION}")


def test_pyproject_version_matches_manifest() -> None:
    pyproject = (PACKAGE.parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{MANIFEST_VERSION}"' in pyproject


def test_panel_script_has_no_hard_coded_version() -> None:
    """The header takes the version from the panel config: no "v0.12.2" left to go stale."""
    script = (PACKAGE / "frontend" / "fdm-printer-panel.js").read_text(encoding="utf-8")
    assert not re.search(r"\bv\d+\.\d+\.\d+\b", script)
    assert "this._panel?.config?.version" in script


async def test_hub_entry_diagnostics(hass: HomeAssistant) -> None:
    """The hub entry has no coordinator: diagnostics used to fail with "no attribute runtime_data"."""
    hub = await _add_hub(hass)
    diag = await async_get_config_entry_diagnostics(hass, hub)
    assert diag["version"] == MANIFEST_VERSION
    assert diag["entry"] == "hub"
    assert diag["panel_registered"] is True
    assert diag["printer_count"] == 0
    assert diag["printers"] == []
    json.dumps(diag)  # it must be serialisable, as HA writes it to a file

    # With a printer: its type and state, never its name or serial.
    set_states(hass, status="idle", energy="0")
    printer = make_entry(hass)
    assert await hass.config_entries.async_setup(printer.entry_id)
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, hub)
    assert diag["printer_count"] == 1
    assert diag["printers"][0]["state"] == "loaded"
    assert SERIAL not in str(diag)
    assert "X2D" not in str(diag)


def test_no_deprecated_device_registry_call() -> None:
    """``async_get_device`` stops working in HA 2027.8: only ``own_device`` may fall back to it."""
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"\.async_get_device\(", text):
            body_start = text.rfind("\ndef ", 0, match.start())
            if not text.startswith("\ndef own_device(", body_start):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.name}:{line}")
    assert offenders == []
