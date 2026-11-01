"""Constants for the FDM Printer Manager integration."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

DOMAIN: Final = "fdm_printer_manager"
NAME: Final = "FDM Printer Manager"

# ha-bambulab integration domain (used to filter devices in the config flow)
BAMBU_DOMAIN: Final = "bambu_lab"

STORAGE_VERSION: Final = 1
STORAGE_KEY_PREFIX: Final = DOMAIN

# --------------------------------------------------------------------------- #
# Config entry keys
# --------------------------------------------------------------------------- #
CONF_PRINTER_TYPE: Final = "printer_type"


class PrinterType(StrEnum):
    """How the integration talks to the printer."""

    KLIPPER = "klipper"  # Moonraker HTTP API, direct
    BAMBU = "bambu"  # through the ha-bambulab integration
    BAMBU_LAN = "bambu_lan"  # Bambu Lab printer over the local network (MQTT), direct


# Direct connection (Klipper today; OctoPrint and Bambu LAN later)
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_API_KEY: Final = "api_key"
CONF_WEB_URL: Final = "web_url"
CONF_ACCESS_CODE: Final = "access_code"  # Bambu LAN

KLIPPER_POLL_INTERVAL: Final = 5  # seconds
WEBCAM_CHECK_INTERVAL: Final = 1800  # seconds: look for a newly installed (or removed) camera

# The "FDM Printers" sidebar panel. A special entry (``panel: True``) makes it
# available before any printer is added; every printer entry also shows it.
CONF_PANEL: Final = "panel"
PANEL_URL_PATH: Final = "fdm-printers"
PANEL_TITLE: Final = "FDM Printers"
PANEL_ICON: Final = "mdi:printer-3d-nozzle"
PANEL_ELEMENT: Final = "fdm-printer-panel"
STATIC_URL: Final = "/fdm_printer_manager_static"

# Safe power: how long to wait for the printer's computer to shut down. Still
# answering after this, the plug is left on and a notification is shown.
SAFE_POWER_WAIT: Final = 300  # seconds
SAFE_POWER_POLL: Final = 5  # seconds between "is it down yet?" checks
SAFE_POWER_DOWN_CHECKS: Final = 3  # checks in a row without an answer = the computer is off
SAFE_POWER_GRACE: Final = 10  # seconds more before the plug is cut (disks, last writes)
# Safe power never cuts a hot nozzle: the hotend fan has to cool the heat break
# first, or the filament softens inside it and can clog the nozzle.
SAFE_POWER_COOL_TEMP: Final = 80  # °C: the plug is cut only below this
SAFE_POWER_COOL_TIMEOUT: Final = 1800  # seconds: still hot after this means a heater is on

# Print history kept per printer (newest first).
HISTORY_LIMIT: Final = 500

ACTIVE_STATES: Final = ("printing", "paused", "running", "pause", "prepare", "slicing")
PAUSED_STATES: Final = ("paused", "pause")

CONF_PRINTER_DEVICE_ID: Final = "printer_device_id"
CONF_PRINTER_NAME: Final = "printer_name"
CONF_PRINTER_SERIAL: Final = "printer_serial"

# Bambu entities
CONF_PRINT_STATUS_ENTITY: Final = "print_status_entity"
CONF_JOB_NAME_ENTITY: Final = "job_name_entity"
CONF_PROGRESS_ENTITY: Final = "progress_entity"
CONF_REMAINING_TIME_ENTITY: Final = "remaining_time_entity"
CONF_COVER_IMAGE_ENTITY: Final = "cover_image_entity"

BAMBU_ENTITY_KEYS: Final = (
    CONF_PRINT_STATUS_ENTITY,
    CONF_JOB_NAME_ENTITY,
    CONF_PROGRESS_ENTITY,
    CONF_REMAINING_TIME_ENTITY,
    CONF_COVER_IMAGE_ENTITY,
)

# Smart plug and electricity (power / energy are optional: a plug without a
# meter still works, there is just no consumption or cost)
CONF_SWITCH_ENTITY: Final = "switch_entity"
CONF_POWER_ENTITY: Final = "power_entity"
CONF_ENERGY_ENTITY: Final = "energy_entity"
CONF_PRICE_ENTITY: Final = "price_entity"
CONF_PRICE_FIXED: Final = "price_per_kwh"  # typed price, used when no price entity is chosen
CONF_INTEGRATION_METHOD: Final = "integration_method"

# Behaviour
CONF_UPDATE_INTERVAL: Final = "update_interval"
CONF_CURRENCY: Final = "currency"
# Minutes after a print ends before the printer is turned off safely (0 = never).
CONF_AUTO_OFF: Final = "auto_off_minutes"
DEFAULT_AUTO_OFF: Final = 15
MAX_AUTO_OFF: Final = 240
# Klipper: SSH login used to shut the computer down when Moonraker cannot (optional)
CONF_SSH_USERNAME: Final = "ssh_username"
CONF_SSH_PASSWORD: Final = "ssh_password"
CONF_SSH_KEY: Final = "ssh_private_key"
CONF_SSH_COMMAND: Final = "ssh_command"
# Klipper: Z offset buttons on the card while printing ("baby steps"); "save" keeps the offset (Z_OFFSET_APPLY_*)
CONF_BABYSTEP: Final = "babystep"
CONF_BABYSTEP_SAVE: Final = "babystep_save"

DEFAULT_UPDATE_INTERVAL: Final = 30  # seconds
MIN_UPDATE_INTERVAL: Final = 10
MAX_UPDATE_INTERVAL: Final = 300

# HTTP
HTTP_TIMEOUT: Final = 10  # seconds
PROBE_TIMEOUT: Final = 3  # seconds per port when looking for Moonraker (7125, then 80)

# Power integration: an interval longer than this (HA restart, sensor outage)
# is still integrated but reported as a gap in the job attributes.
MAX_SAMPLE_GAP: Final = 300  # seconds

EVENT_PRINT_FINISHED: Final = f"{DOMAIN}_print_finished"
EVENT_TYPE_PRINT_FINISHED: Final = "print_finished"
EVENT_PRINT_MARKED: Final = f"{DOMAIN}_print_marked"  # a finished print was marked success / failed by hand

SERVICE_MARK_PRINT: Final = "mark_print"
SERVICE_BABYSTEP: Final = "babystep"

# Watching a running print (printer down, stuck or interrupted print, no progress)
EVENT_ALERT: Final = f"{DOMAIN}_alert"
CONF_ALERTS: Final = "alerts"  # on / off
CONF_DOWN_MINUTES: Final = "down_minutes"
CONF_CLOSE_MINUTES: Final = "close_minutes"
CONF_STALL_MINUTES: Final = "stall_minutes"  # 0: no "no progress" warning
CONF_NOTIFY: Final = "notify_services"  # "notify.mobile_app_x, notify.mobile_app_y"
DEFAULT_DOWN_MINUTES: Final = 2
DEFAULT_CLOSE_MINUTES: Final = 10
DEFAULT_STALL_MINUTES: Final = 60

# When a print ends: a to-do item ("remove the print") and / or a script
CONF_TODO_ENTITY: Final = "todo_entity"
CONF_END_SCRIPT: Final = "end_script"
# ... and / or one row in a Google Sheet (Home Assistant's google_sheets integration)
CONF_SHEETS_ENTRY: Final = "sheets_config_entry"
CONF_SHEETS_WORKSHEET: Final = "sheets_worksheet"
DEFAULT_SHEETS_WORKSHEET: Final = "Prints"


class IntegrationMethod(StrEnum):
    """How power samples are integrated into energy."""

    LEFT = "left"
    TRAPEZOIDAL = "trapezoidal"


DEFAULT_INTEGRATION_METHOD: Final = IntegrationMethod.LEFT
