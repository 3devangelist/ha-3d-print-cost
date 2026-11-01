"""Read a Bambu Lab printer's IP address, serial number and access code from a photo of its screen.

Two ways, chosen by the panel:
- the Home Assistant AI Task the user already has (``ai_task.generate_data`` with the photo attached);
- without one, the panel reads the text in the browser (Tesseract.js) and sends only that text here.

Both end in :func:`normalise_fields`, so the form gets the same clean values either way.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

AI_TASK_DOMAIN = "ai_task"
# AITaskEntityFeature.GENERATE_DATA | SUPPORT_ATTACHMENTS
_AI_FEATURES = 1 | 2
TMP_DIR = "fdm_tmp"
MAX_PHOTO_BYTES = 6 * 1024 * 1024

_IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\s*[.,]\s*(\d{1,3})\s*[.,]\s*(\d{1,3})\s*[.,]\s*(\d{1,3})(?![\d.])")
# Labels on the printer's "LAN Only" screen and in its settings, as read by a camera or OCR.
_IP_LABEL = re.compile(r"\bIP(?:\s*address)?\b\s*[:=]?\s*", re.I)
_CODE_LABEL = re.compile(r"\b(?:access\s*code|code)\b\s*[:=]?\s*([A-Za-z0-9 ]{8,12})", re.I)
_SERIAL_LABEL = re.compile(r"\b(?:SN|S/N|serial(?:\s*number)?)\b\s*[:=]?\s*([A-Za-z0-9 ]{15,24})", re.I)
# Bambu serial numbers: 15 or 18 letters/digits, always starting with a digit.
_SERIAL_BARE = re.compile(r"\b(\d[A-Z0-9]{14}(?:[A-Z0-9]{3})?)\b")

INSTRUCTIONS = (
    "This is a photo of a Bambu Lab 3D printer screen or label. Read three values exactly as printed: "
    "the IP address (IPv4, like 192.168.1.50), the serial number (SN, 15 or 18 letters and digits) "
    "and the LAN access code (8 characters, case-sensitive). Leave a value empty if you cannot see it. "
    "Do not guess."
)
STRUCTURE = {
    "ip": {"description": "IPv4 address shown on the printer", "selector": {"text": {}}},
    "serial": {"description": "Serial number (SN), 15 or 18 characters", "selector": {"text": {}}},
    "access_code": {"description": "LAN access code, 8 characters", "selector": {"text": {}}},
}


def find_ai_entity(hass: HomeAssistant) -> tuple[str, str] | None:
    """The first AI Task entity that can generate data from an attachment: (entity_id, name)."""
    for state in sorted(hass.states.async_all(AI_TASK_DOMAIN), key=lambda s: s.entity_id):
        if state.state == "unavailable":
            continue
        if int(state.attributes.get("supported_features", 0)) & _AI_FEATURES == _AI_FEATURES:
            return state.entity_id, str(state.attributes.get("friendly_name") or state.entity_id)
    return None


def _clean_ip(value: str) -> str:
    match = _IPV4.search(value or "")
    if not match:
        return ""
    parts = [int(p) for p in match.groups()]
    return ".".join(map(str, parts)) if all(0 <= p <= 255 for p in parts) else ""


def normalise_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """Clean what was read and say which values look wrong (the form still accepts them)."""
    ip = _clean_ip(str(raw.get("ip") or ""))
    serial = re.sub(r"[^A-Za-z0-9]", "", str(raw.get("serial") or "")).upper()
    code = re.sub(r"\s", "", str(raw.get("access_code") or ""))
    fields = {k: v for k, v in (("host", ip), ("printer_serial", serial), ("access_code", code)) if v}
    warnings = []
    if raw.get("ip") and not ip:
        warnings.append("host")
    if serial and len(serial) not in (15, 18):
        warnings.append("printer_serial")
    if code and len(code) != 8:
        warnings.append("access_code")
    return {"fields": fields, "warnings": warnings}


def parse_screen_text(text: str) -> dict[str, str]:
    """Find the three values in the text an OCR read from the printer's screen."""
    raw = {"ip": "", "serial": "", "access_code": ""}
    for line in (text or "").splitlines():
        if not raw["ip"] and _IP_LABEL.search(line) and (ip := _clean_ip(line)):
            raw["ip"] = ip
        if not raw["access_code"] and (m := _CODE_LABEL.search(line)):
            raw["access_code"] = m.group(1).replace(" ", "")[:8]
        if not raw["serial"] and (m := _SERIAL_LABEL.search(line)):
            raw["serial"] = m.group(1).replace(" ", "")
    if not raw["ip"]:
        raw["ip"] = _clean_ip(text or "")
    if not raw["serial"] and (m := _SERIAL_BARE.search((text or "").upper())):
        raw["serial"] = m.group(1)
    return raw


def decode_photo(data: str) -> bytes:
    """The photo sent by the panel (base64, optionally a data: URL)."""
    if data.startswith("data:"):
        data = data.split(",", 1)[-1]
    try:
        photo = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as err:
        raise ValueError("not_an_image") from err
    if not photo or len(photo) > MAX_PHOTO_BYTES:
        raise ValueError("too_large" if photo else "not_an_image")
    return photo


def _media_dir(hass: HomeAssistant) -> tuple[str, Path]:
    dirs = hass.config.media_dirs or {}
    key = "local" if "local" in dirs else next(iter(dirs), None)
    if key is None:
        raise HomeAssistantError("no_media_dir")
    return key, Path(dirs[key])


async def async_read_with_ai(hass: HomeAssistant, entity_id: str, photo: bytes, mime: str) -> dict[str, Any]:
    """Ask the AI Task to read the photo. The temporary copy is always deleted afterwards."""
    key, base = _media_dir(hass)
    ext = ".png" if mime == "image/png" else ".jpg"
    name = f"{uuid.uuid4().hex}{ext}"
    path = base / TMP_DIR / name
    media_id = f"media-source://media_source/{key}/{TMP_DIR}/{name}"

    def _write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(photo)

    await hass.async_add_executor_job(_write)
    try:
        response = await hass.services.async_call(
            AI_TASK_DOMAIN,
            "generate_data",
            {
                "task_name": "FDM Printer Manager: read Bambu Lab printer details",
                "entity_id": entity_id,
                "instructions": INSTRUCTIONS,
                "structure": STRUCTURE,
                "attachments": [{"media_content_id": media_id, "media_content_type": mime}],
            },
            blocking=True,
            return_response=True,
        )
    finally:
        await hass.async_add_executor_job(path.unlink, True)
    data = (response or {}).get("data") or {}
    return data if isinstance(data, dict) else {}
