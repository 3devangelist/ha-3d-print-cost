"""FDM Printer Manager — your 3D printers in Home Assistant, with the energy and cost of every print.

Printers: Klipper (Moonraker, direct) and Bambu Lab (through ha-bambulab).
Optional: a smart plug with a meter + an electricity price for the cost of each print.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components import frontend, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from . import photo
from .adapters.bambu_lan import BambuAnnouncement, async_listen_ssdp
from .const import (
    CONF_AUTO_OFF,
    CONF_HOST,
    CONF_PANEL,
    CONF_PRINTER_NAME,
    CONF_PRINTER_SERIAL,
    CONF_PRINTER_TYPE,
    DOMAIN,
    LEGACY_AUTO_OFF_MINUTES,
    PANEL_ELEMENT,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL_PATH,
    STATIC_URL,
    PrinterType,
)
from .coordinator import PrintCostConfigEntry, PrintCostCoordinator, own_device
from .finish_notify import panel_options
from .materials import LABELS, MATERIALS, SOURCE_FILE_NAME, generic_prices, material_from_file_name
from .naming import bambu_model, bambu_name, friendly_name, is_generic
from .outcome import history_stats
from .scanner import async_scan_network, async_start_scanning
from .services import async_setup_services
from .sheets import async_setup_log, async_stop_log, sheets_status

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.IMAGE,
    Platform.EVENT,
    Platform.SWITCH,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.LIGHT,
]

# Entities of the removed history and G-code estimate features.
REMOVED_KEYS = ("print_history", "gcode_estimate", "gcode_file", "estimate_gcode", "refresh_gcode_files")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions (they work for every printer entry)."""
    async_setup_services(hass)
    return True


def migrate_auto_off(conf: Mapping[str, Any]) -> dict[str, Any]:
    """1.1 -> 1.2: ``auto_off_minutes`` (0 = never) becomes the on/off ``auto_off`` (more than 0 -> on)."""
    new = dict(conf)
    if LEGACY_AUTO_OFF_MINUTES in new:
        minutes = new.pop(LEGACY_AUTO_OFF_MINUTES)
        try:
            enabled = float(minutes or 0) > 0
        except (TypeError, ValueError):
            enabled = True  # unreadable: keep the default
        new.setdefault(CONF_AUTO_OFF, enabled)
    return new


async def async_migrate_entry(hass: HomeAssistant, entry: PrintCostConfigEntry) -> bool:
    """Migrate an entry made by an older version."""
    if entry.version > 1:
        return False  # made by a newer version: do not touch it
    if entry.minor_version < 2:
        hass.config_entries.async_update_entry(
            entry, data=migrate_auto_off(entry.data), options=migrate_auto_off(entry.options), minor_version=2
        )
        _LOGGER.info("%s: auto-off minutes turned into on/off (%s)", entry.title, entry.options.get(CONF_AUTO_OFF))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PrintCostConfigEntry) -> bool:
    """Set up one printer (or only the sidebar panel)."""
    await _async_ensure_panel(hass)
    if entry.data.get(CONF_PANEL):
        return True
    registry = er.async_get(hass)
    for key in REMOVED_KEYS:
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
            if entity.unique_id == f"{entry.entry_id}_{key}":
                registry.async_remove(entity.entity_id)
    _rename_generic(hass, entry)
    coordinator = PrintCostCoordinator(hass, entry)
    await coordinator.async_start()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def better_name(entry: PrintCostConfigEntry) -> str | None:
    """A readable name for a printer still called by a shared hostname ("cosmos", "micromachine"), else ``None``."""
    if not is_generic(entry.title):
        return None
    conf = {**entry.data, **entry.options}
    if (conf.get(CONF_PRINTER_TYPE) or PrinterType.BAMBU) == PrinterType.KLIPPER:
        name = friendly_name(entry.title, str(conf.get(CONF_HOST) or ""))
    elif bambu_model(conf.get(CONF_PRINTER_SERIAL)):
        name = bambu_name(conf.get(CONF_PRINTER_SERIAL))
    else:
        return None
    return name if name != entry.title else None


def _rename_generic(hass: HomeAssistant, entry: PrintCostConfigEntry) -> None:
    """Rename a printer added under a shared hostname.

    A name the user gave the device is kept, and entity IDs never change.
    """
    if (name := better_name(entry)) is None:
        return
    _LOGGER.info("Renaming printer %s to %s", entry.title, name)
    options = {**entry.options, CONF_PRINTER_NAME: name} if CONF_PRINTER_NAME in entry.options else entry.options
    hass.config_entries.async_update_entry(
        entry, title=name, data={**entry.data, CONF_PRINTER_NAME: name}, options=options
    )
    registry = dr.async_get(hass)
    device = own_device(registry, entry.entry_id)
    if device is not None and device.name_by_user is None:
        registry.async_update_device(device.id, name=name)


async def async_unload_entry(hass: HomeAssistant, entry: PrintCostConfigEntry) -> bool:
    """Unload: the active job is flushed to storage and resumed on next setup."""
    if entry.data.get(CONF_PANEL):
        unloaded = True
    else:
        unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        if unloaded:
            await entry.runtime_data.async_stop()
    if unloaded:
        _remove_panel_if_unused(hass, entry)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: PrintCostConfigEntry) -> None:
    """Delete stored jobs and the last print image when the integration is removed."""
    if entry.data.get(CONF_PANEL):
        return
    from .storage import JobStore  # noqa: PLC0415 - only needed here

    await JobStore(hass, entry.entry_id).async_remove()


async def _async_update_listener(hass: HomeAssistant, entry: PrintCostConfigEntry) -> None:
    """Options changed: reload (the running print survives through storage)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_ensure_panel(hass: HomeAssistant) -> None:
    """Register the "FDM Printers" sidebar panel (and its websocket command) once."""
    data = hass.data.setdefault(DOMAIN, {})
    if not data.get("ws"):
        websocket_api.async_register_command(hass, ws_history)
        websocket_api.async_register_command(hass, ws_scan)
        websocket_api.async_register_command(hass, ws_photo_ai)
        websocket_api.async_register_command(hass, ws_read_photo)
        websocket_api.async_register_command(hass, ws_parse_photo_text)
        websocket_api.async_register_command(hass, ws_sheets_status)
        websocket_api.async_register_command(hass, ws_sheets_setup)
        websocket_api.async_register_command(hass, ws_sheets_off)
        data["ws"] = True
    if "ssdp" not in data:
        data["ssdp"] = []  # set before awaiting so two entries never start two listeners
        data["ssdp"] = await async_listen_ssdp(lambda found: _on_bambu_found(hass, found))
    if "scan_stop" not in data:
        data["scan_stop"] = async_start_scanning(hass)
    if data.get("panel"):
        return
    if not data.get("static") and hass.http is not None:
        frontend_dir = Path(__file__).parent / "frontend"
        await hass.http.async_register_static_paths([StaticPathConfig(STATIC_URL, str(frontend_dir), False)])
        data["static"] = True
    version = await hass.async_add_executor_job(_manifest_version)
    frontend.async_register_built_in_panel(
        hass,
        component_name="custom",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        frontend_url_path=PANEL_URL_PATH,
        config={
            # The panel shows this in its header: always the installed manifest.json version.
            "version": version,
            "_panel_custom": {
                "name": PANEL_ELEMENT,
                "module_url": f"{STATIC_URL}/{PANEL_ELEMENT}.js?v={version}",
                "embed_iframe": False,
                "trust_external": False,
            },
        },
        require_admin=True,
        update=True,
    )
    data["panel"] = True


def _remove_panel_if_unused(hass: HomeAssistant, unloading: PrintCostConfigEntry) -> None:
    """Take the panel out of the sidebar when no entry of this integration is loaded any more."""
    others = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != unloading.entry_id and e.state is ConfigEntryState.LOADED
    ]
    data = hass.data.get(DOMAIN, {})
    if not others and data.get("panel"):
        frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
        data["panel"] = False
    if not others and "ssdp" in data:
        for transport in data.pop("ssdp") or []:
            transport.close()
    if not others and "scan_stop" in data:
        data.pop("scan_stop")()


def _on_bambu_found(hass: HomeAssistant, found: BambuAnnouncement) -> None:
    """A Bambu Lab printer announced itself: offer it once per run (HA dedupes by serial too)."""
    seen: set[str] = hass.data.setdefault(DOMAIN, {}).setdefault("seen", set())
    if found.serial in seen:
        return
    seen.add(found.serial)
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_INTEGRATION_DISCOVERY},
            data={
                CONF_HOST: found.host,
                CONF_PRINTER_SERIAL: found.serial,
                CONF_PRINTER_NAME: bambu_name(found.serial, found.model, found.name),
            },
        )
    )


def _manifest_version() -> str:
    """Version string used to bust the browser cache of the panel script."""
    import json  # noqa: PLC0415

    manifest = json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))
    return str(manifest.get("version", "0"))


def _with_material(row: dict) -> dict:
    """A print saved without a material (before 0.15.0) shows the one written in its file name, if any.

    Only for the answer to the panel: the saved history is not changed. Choosing a material in the panel
    saves it as usual.
    """
    if not row.get("filament_type"):
        material = material_from_file_name(row.get("name"))
        if material:
            row["filament_type"] = material
            row["filament_type_source"] = SOURCE_FILE_NAME
    return row


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/history",
        vol.Optional("entry_id"): str,
        vol.Optional("limit", default=50): vol.All(int, vol.Range(min=1, max=500)),
        vol.Optional("offset", default=0): vol.All(int, vol.Range(min=0)),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_history(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Finished prints of every printer (or one), newest first."""
    rows: list[dict] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_PANEL) or entry.state is not ConfigEntryState.LOADED:
            continue
        if msg.get("entry_id") and entry.entry_id != msg["entry_id"]:
            continue
        coordinator: PrintCostCoordinator = entry.runtime_data
        currency = coordinator.currency
        rows.extend(
            _with_material({"entry_id": entry.entry_id, "currency": currency, **h}) for h in coordinator.state.history
        )
    rows.sort(key=lambda r: r.get("finished_at") or "", reverse=True)
    start = msg["offset"]
    prices = generic_prices(panel_options(hass))
    materials = [
        {"name": m, "label": LABELS.get(m, m), "price": float(prices[m]) if m in prices else None} for m in MATERIALS
    ]
    connection.send_result(
        msg["id"],
        {
            "total": len(rows),
            "rows": rows[start : start + msg["limit"]],
            "stats": history_stats(rows),
            "materials": materials,
        },
    )


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/scan"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_scan(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Look for Klipper printers now ("Search again" / "Add printer"); they show up in "Found on the network"."""
    found = await async_scan_network(hass)
    connection.send_result(msg["id"], {"found": [{"host": f.host, "port": f.port} for f in found]})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/photo_ai"})
@websocket_api.require_admin
@callback
def ws_photo_ai(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """The AI Task that "Read from photo" would use, so the panel can say where the photo goes."""
    found = photo.find_ai_entity(hass)
    connection.send_result(msg["id"], {"entity_id": found[0], "name": found[1]} if found else None)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/read_photo",
        vol.Required("image"): str,
        vol.Optional("mime", default="image/jpeg"): vol.In(["image/jpeg", "image/png"]),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_read_photo(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Read the printer's IP, serial and access code from a photo with the user's AI Task."""
    found = photo.find_ai_entity(hass)
    if not found:
        connection.send_result(msg["id"], {"status": "no_ai"})
        return
    try:
        image = photo.decode_photo(msg["image"])
    except ValueError as err:
        connection.send_result(msg["id"], {"status": "error", "message": str(err)})
        return
    try:
        raw = await photo.async_read_with_ai(hass, found[0], image, msg["mime"])
    except Exception as err:  # noqa: BLE001 - any AI provider error: the panel falls back to reading on the device
        _LOGGER.warning("Reading the photo with %s failed: %s", found[0], err)
        connection.send_result(msg["id"], {"status": "error", "message": str(err)})
        return
    result = photo.normalise_fields(raw)
    connection.send_result(msg["id"], {"status": "ok", "source": "ai", "entity": found[1], **result})


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/parse_photo_text", vol.Required("text"): vol.All(str, vol.Length(max=20000))}
)
@websocket_api.require_admin
@callback
def ws_parse_photo_text(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Find the values in the text the browser read from the photo (the photo itself never leaves the browser)."""
    result = photo.normalise_fields(photo.parse_screen_text(msg["text"]))
    connection.send_result(msg["id"], {"status": "ok" if result["fields"] else "empty", "source": "ocr", **result})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/sheets_status"})
@websocket_api.require_admin
@callback
def ws_sheets_status(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """The Google Sheets entries and where each printer logs (the panel's "Google Sheets" button)."""
    connection.send_result(msg["id"], sheets_status(hass))


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/sheets_setup",
        vol.Optional("config_entry"): str,
        vol.Optional("worksheet"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_sheets_setup(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Log every printer to a tab of a Google Sheets entry; the tab is created when it is missing."""
    try:
        result = await async_setup_log(hass, msg.get("config_entry"), msg.get("worksheet"))
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "sheets_setup_failed", str(err))
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/sheets_off"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_sheets_off(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Stop the Google Sheets log of every printer."""
    connection.send_result(msg["id"], {"stopped": await async_stop_log(hass)})
