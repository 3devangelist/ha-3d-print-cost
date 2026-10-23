"""Constants for the 3D Print Cost integration."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

DOMAIN: Final = "ha_3d_print_cost"
NAME: Final = "3D Print Cost"

# ha-bambulab integration domain (used to filter devices in the config flow)
BAMBU_DOMAIN: Final = "bambu_lab"

STORAGE_VERSION: Final = 1
STORAGE_KEY_PREFIX: Final = DOMAIN

# --------------------------------------------------------------------------- #
# Config entry keys
# --------------------------------------------------------------------------- #
CONF_PRINTER_DEVICE_ID: Final = "printer_device_id"
CONF_PRINTER_NAME: Final = "printer_name"
CONF_PRINTER_SERIAL: Final = "printer_serial"

# Bambu entities
CONF_PRINT_STATUS_ENTITY: Final = "print_status_entity"
CONF_JOB_NAME_ENTITY: Final = "job_name_entity"
CONF_PROGRESS_ENTITY: Final = "progress_entity"
CONF_PRINT_WEIGHT_ENTITY: Final = "print_weight_entity"
CONF_REMAINING_TIME_ENTITY: Final = "remaining_time_entity"
CONF_COVER_IMAGE_ENTITY: Final = "cover_image_entity"

BAMBU_ENTITY_KEYS: Final = (
    CONF_PRINT_STATUS_ENTITY,
    CONF_JOB_NAME_ENTITY,
    CONF_PROGRESS_ENTITY,
    CONF_PRINT_WEIGHT_ENTITY,
    CONF_REMAINING_TIME_ENTITY,
    CONF_COVER_IMAGE_ENTITY,
)

# Electricity
CONF_POWER_ENTITY: Final = "power_entity"
CONF_ENERGY_ENTITY: Final = "energy_entity"
CONF_PRICE_ENTITY: Final = "price_entity"
CONF_INTEGRATION_METHOD: Final = "integration_method"

# Filament (v0.2)
CONF_HASPELSYNC_URL: Final = "haspelsync_url"
CONF_HASPELSYNC_API_KEY: Final = "haspelsync_api_key"
CONF_HASPELSYNC_PRINTER_ID: Final = "haspelsync_printer_id"
CONF_SPOOLMAN_URL: Final = "spoolman_url"
CONF_FILAMENT_FALLBACK: Final = "filament_fallback"

# Behaviour
CONF_UPDATE_INTERVAL: Final = "update_interval"
CONF_HISTORY_SIZE: Final = "history_size"
CONF_CURRENCY: Final = "currency"

DEFAULT_UPDATE_INTERVAL: Final = 30  # seconds
MIN_UPDATE_INTERVAL: Final = 10
MAX_UPDATE_INTERVAL: Final = 300
DEFAULT_HISTORY_SIZE: Final = 100
MIN_HISTORY_SIZE: Final = 10
MAX_HISTORY_SIZE: Final = 1000

# HTTP
HTTP_TIMEOUT: Final = 10  # seconds

# Filament resolution after the end of a print. Haspelsync books the usage a
# few seconds after the printer reports a terminal state, so we wait for it.
FILAMENT_RESOLVE_ATTEMPTS: Final = 8
FILAMENT_RESOLVE_DELAY: Final = 15  # seconds between attempts

# Power integration: an interval longer than this (HA restart, sensor outage)
# is still integrated but reported as a gap in the job attributes.
MAX_SAMPLE_GAP: Final = 300  # seconds

EVENT_PRINT_FINISHED: Final = f"{DOMAIN}_print_finished"
EVENT_TYPE_PRINT_FINISHED: Final = "print_finished"


class IntegrationMethod(StrEnum):
    """How power samples are integrated into energy."""

    LEFT = "left"
    TRAPEZOIDAL = "trapezoidal"


class FilamentFallback(StrEnum):
    """What to do when no real consumption is available."""

    # Never estimate. Weight/cost stay unknown.
    NONE = "none"
    # Use the sliced (planned) weight for successful prints only.
    PLANNED_SUCCESS = "planned_success"
    # Same, plus planned weight x progress for failed / cancelled prints.
    PLANNED_PROGRESS = "planned_progress"


DEFAULT_INTEGRATION_METHOD: Final = IntegrationMethod.LEFT
DEFAULT_FILAMENT_FALLBACK: Final = FilamentFallback.PLANNED_SUCCESS
