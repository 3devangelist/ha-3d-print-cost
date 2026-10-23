"""ha-bambulab adapter: entity discovery and state reading.

Never relies on ``entity_id`` strings (users rename them, and they are
localised). Entities are found through the entity registry by:

1. the printer's ``device_id``,
2. the ``bambu_lab`` platform,
3. the ``translation_key`` (e.g. ``print_status``),
4. falling back to the ``unique_id`` suffix (``<serial>_<key>``).

The user can always correct the result in the config flow.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from ..const import (
    BAMBU_DOMAIN,
    CONF_COVER_IMAGE_ENTITY,
    CONF_JOB_NAME_ENTITY,
    CONF_PRINT_STATUS_ENTITY,
    CONF_PRINT_WEIGHT_ENTITY,
    CONF_PROGRESS_ENTITY,
    CONF_REMAINING_TIME_ENTITY,
)
from ..units import UnsupportedUnitError, mass_to_g, to_float

_LOGGER = logging.getLogger(__name__)

# config key -> (entity domain, candidate keys by preference)
DISCOVERY_KEYS: dict[str, tuple[str, tuple[str, ...]]] = {
    CONF_PRINT_STATUS_ENTITY: ("sensor", ("print_status",)),
    CONF_JOB_NAME_ENTITY: ("sensor", ("subtask_name", "gcode_file")),
    CONF_PROGRESS_ENTITY: ("sensor", ("print_progress",)),
    CONF_PRINT_WEIGHT_ENTITY: ("sensor", ("print_weight",)),
    CONF_REMAINING_TIME_ENTITY: ("sensor", ("remaining_time",)),
    CONF_COVER_IMAGE_ENTITY: ("image", ("cover_image",)),
}

_INVALID = {None, "", STATE_UNKNOWN, STATE_UNAVAILABLE}


def printer_serial(hass: HomeAssistant, device_id: str) -> str | None:
    """Serial number of a ha-bambulab printer device (its identifier)."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    for domain, identifier in device.identifiers:
        if domain == BAMBU_DOMAIN:
            return str(identifier)
    return None


def printer_name(hass: HomeAssistant, device_id: str) -> str | None:
    """Display name of the device."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    return device.name_by_user or device.name


def discover_entities(hass: HomeAssistant, device_id: str) -> dict[str, str]:
    """Return ``{config_key: entity_id}`` for what could be found."""
    registry = er.async_get(hass)
    entries = er.async_entries_for_device(registry, device_id, include_disabled_entities=False)
    found: dict[str, str] = {}

    for conf_key, (domain, keys) in DISCOVERY_KEYS.items():
        candidates = [e for e in entries if e.domain == domain]
        bambu = [e for e in candidates if e.platform == BAMBU_DOMAIN] or candidates
        match = None
        for key in keys:
            match = next((e for e in bambu if e.translation_key == key), None) or next(
                (e for e in bambu if (e.unique_id or "").endswith(f"_{key}")), None
            )
            if match:
                break
        if match:
            found[conf_key] = match.entity_id

    _LOGGER.debug("Discovered Bambu entities for %s: %s", device_id, found)
    return found


# --------------------------------------------------------------------------- #
# State reading
# --------------------------------------------------------------------------- #
def read_text(hass: HomeAssistant, entity_id: str | None) -> str | None:
    """State as text, ``None`` when unknown / unavailable."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in _INVALID:
        return None
    return state.state


def read_raw_status(hass: HomeAssistant, entity_id: str | None) -> str | None:
    """Print status, keeping 'unavailable' so the tracker can ignore it."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    return None if state is None else state.state


def read_number(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Numeric state, ``None`` when unknown."""
    text = read_text(hass, entity_id)
    return to_float(text)


def read_print_weight(hass: HomeAssistant, entity_id: str | None) -> tuple[float | None, dict[str, float]]:
    """Total sliced weight (g) and per-slot weights from the attributes.

    ha-bambulab exposes ``{"AMS 1 Tray 1": 12.3, ...}`` (or
    ``{"External Spool": ...}``) as attributes of ``print_weight``.
    """
    if not entity_id:
        return None, {}
    state = hass.states.get(entity_id)
    if state is None:
        return None, {}
    unit = state.attributes.get("unit_of_measurement")
    total = to_float(state.state)
    try:
        total_g = mass_to_g(total, unit) if total is not None else None
    except UnsupportedUnitError:
        total_g = None
    slots: dict[str, float] = {}
    for key, value in state.attributes.items():
        if not isinstance(key, str):
            continue
        if not (key.startswith("AMS ") or key.startswith("External Spool")):
            continue
        grams = to_float(value)
        if grams is not None and grams > 0:
            slots[key] = grams
    return total_g, slots


def read_remaining_minutes(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Remaining time converted to minutes."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    value = to_float(state.state)
    if value is None:
        return None
    unit = (state.attributes.get("unit_of_measurement") or "min").lower()
    factors: dict[str, float] = {"min": 1, "h": 60, "s": 1 / 60, "d": 1440}
    return value * factors.get(unit, 1)


def attributes_of(hass: HomeAssistant, entity_id: str | None) -> dict[str, Any]:
    """Attributes of an entity (empty dict if missing)."""
    if not entity_id:
        return {}
    state = hass.states.get(entity_id)
    return dict(state.attributes) if state else {}
