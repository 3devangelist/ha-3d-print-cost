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
    BAMBU = "bambu"  # through the ha-bambulab integration (direct LAN mode planned)


# Direct connection (Klipper today; OctoPrint and Bambu LAN later)
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_API_KEY: Final = "api_key"
CONF_WEB_URL: Final = "web_url"

KLIPPER_POLL_INTERVAL: Final = 5  # seconds

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
CONF_INTEGRATION_METHOD: Final = "integration_method"

# Behaviour
CONF_UPDATE_INTERVAL: Final = "update_interval"
CONF_CURRENCY: Final = "currency"

DEFAULT_UPDATE_INTERVAL: Final = 30  # seconds
MIN_UPDATE_INTERVAL: Final = 10
MAX_UPDATE_INTERVAL: Final = 300

# HTTP
HTTP_TIMEOUT: Final = 10  # seconds

# Power integration: an interval longer than this (HA restart, sensor outage)
# is still integrated but reported as a gap in the job attributes.
MAX_SAMPLE_GAP: Final = 300  # seconds

EVENT_PRINT_FINISHED: Final = f"{DOMAIN}_print_finished"
EVENT_TYPE_PRINT_FINISHED: Final = "print_finished"


class IntegrationMethod(StrEnum):
    """How power samples are integrated into energy."""

    LEFT = "left"
    TRAPEZOIDAL = "trapezoidal"


DEFAULT_INTEGRATION_METHOD: Final = IntegrationMethod.LEFT
