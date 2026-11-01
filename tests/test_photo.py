"""Read from photo (Bambu LAN): the AI Task path, the on-device (OCR text) path and the clean-up of the values."""

import base64
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError

from custom_components.fdm_printer_manager.const import DOMAIN
from custom_components.fdm_printer_manager.photo import (
    TMP_DIR,
    decode_photo,
    normalise_fields,
    parse_screen_text,
)

# Invented values (not a real printer).
PHOTO = b"\xff\xd8\xff\xe0fake-jpeg-bytes\xff\xd9"
PHOTO_B64 = base64.b64encode(PHOTO).decode()

VALUES = {"ip": "192.168.1.123", "serial": "0309AB123456789", "access_code": "4f7a9c2e"}

SCREEN_TEXT = """LAN Only Mode
LAN Only Mode  ON
IP: 192.168.1.123
Access Code: 4f7a 9c2e
Developer Mode  ON
SN: 0309AB123456789
"""


def test_normalise_cleans_the_values() -> None:
    out = normalise_fields({"ip": " 192.168.001.050 ", "serial": "0309 ab12-3456 789", "access_code": "4f7a 9c2e"})
    assert out == {
        "fields": {
            "host": "192.168.1.50",
            "printer_serial": "0309AB123456789",
            "access_code": "4f7a9c2e",
            "printer_name": "Bambu Lab A1 mini",  # the model, from the serial number
        },
        "warnings": [],
    }


def test_normalise_flags_values_that_look_wrong_but_keeps_them() -> None:
    out = normalise_fields({"ip": "192.168.1.300", "serial": "0309AB12", "access_code": "4f7a9c2e1"})
    assert out["fields"] == {"printer_serial": "0309AB12", "access_code": "4f7a9c2e1"}
    assert sorted(out["warnings"]) == ["access_code", "host", "printer_serial"]


def test_normalise_empty() -> None:
    assert normalise_fields({}) == {"fields": {}, "warnings": []}


def test_parse_screen_text_finds_the_three_values() -> None:
    assert parse_screen_text(SCREEN_TEXT) == VALUES


def test_parse_screen_text_as_tesseract_reads_it() -> None:
    # Output of Tesseract.js 5.1.1 (the panel's reader) for a blurred, slightly rotated photo of a LAN Only page.
    text = (
        "LAN Only Mode\n\nLAN Only Mode ON\n\nIP 192.168.1.123\nAccess Code 4f7a9c2e\n"
        "Developer Mode ON\n\nSN 0309AB123456789\n"
    )
    assert parse_screen_text(text) == VALUES


def test_parse_screen_text_without_labels() -> None:
    raw = parse_screen_text("Network 192.168.1.77\nsomething 01S00C123456789\n")
    assert raw["ip"] == "192.168.1.77"
    assert raw["serial"] == "01S00C123456789"
    assert raw["access_code"] == ""


def test_parse_screen_text_ocr_noise() -> None:
    raw = parse_screen_text("IP address 192 . 168 . 1 . 9\nS/N 0309AB123456789ABC\naccess code : ab12cd34")
    assert raw == {"ip": "192.168.1.9", "serial": "0309AB123456789ABC", "access_code": "ab12cd34"}


def test_decode_photo() -> None:
    assert decode_photo(PHOTO_B64) == PHOTO
    assert decode_photo(f"data:image/jpeg;base64,{PHOTO_B64}") == PHOTO
    with pytest.raises(ValueError, match="not_an_image"):
        decode_photo("not base64 !!")


@pytest.fixture
async def ws(hass: HomeAssistant, hass_ws_client, panel_entry, tmp_path: Path):
    """The integration's websocket commands, with a temporary media folder."""
    hass.config.media_dirs = {"local": str(tmp_path)}
    assert await hass.config_entries.async_setup(panel_entry.entry_id)
    await hass.async_block_till_done()
    return await hass_ws_client(hass)


def _ai_entity(hass: HomeAssistant, features: int = 3) -> None:
    attrs = {"friendly_name": "OpenAI AI Task", "supported_features": features}
    hass.states.async_set("ai_task.openai", "unknown", attrs)


async def _call(ws, msg_id: int, **msg) -> dict:
    await ws.send_json({"id": msg_id, **msg})
    reply = await ws.receive_json()
    assert reply["success"], reply
    return reply["result"]


async def test_photo_ai_none_and_found(hass: HomeAssistant, ws) -> None:
    assert await _call(ws, 1, type=f"{DOMAIN}/photo_ai") is None
    _ai_entity(hass, features=1)  # cannot take attachments
    assert await _call(ws, 2, type=f"{DOMAIN}/photo_ai") is None
    _ai_entity(hass)
    assert await _call(ws, 3, type=f"{DOMAIN}/photo_ai") == {"entity_id": "ai_task.openai", "name": "OpenAI AI Task"}


async def test_read_photo_without_ai_tells_the_panel_to_read_on_the_device(ws) -> None:
    assert await _call(ws, 1, type=f"{DOMAIN}/read_photo", image=PHOTO_B64) == {"status": "no_ai"}


async def test_read_photo_with_ai_and_the_photo_is_deleted(hass: HomeAssistant, ws, tmp_path: Path) -> None:
    _ai_entity(hass)
    seen: list[ServiceCall] = []

    async def generate_data(call: ServiceCall) -> dict:
        seen.append(call)
        media_id = call.data["attachments"][0]["media_content_id"]
        assert media_id.startswith(f"media-source://media_source/local/{TMP_DIR}/")
        photo = tmp_path / TMP_DIR / media_id.rsplit("/", 1)[-1]
        assert photo.read_bytes() == PHOTO  # there while the AI reads it
        return {"conversation_id": "x", "data": {**VALUES, "serial": "0309ab123456789"}}

    hass.services.async_register("ai_task", "generate_data", generate_data, supports_response=SupportsResponse.ONLY)
    result = await _call(ws, 1, type=f"{DOMAIN}/read_photo", image=PHOTO_B64)

    assert result == {
        "status": "ok",
        "source": "ai",
        "entity": "OpenAI AI Task",
        "fields": {
            "host": "192.168.1.123",
            "printer_serial": "0309AB123456789",
            "access_code": "4f7a9c2e",
            "printer_name": "Bambu Lab A1 mini",
        },
        "warnings": [],
    }
    assert seen[0].data["entity_id"] == "ai_task.openai"
    assert set(seen[0].data["structure"]) == {"ip", "serial", "access_code"}
    assert list((tmp_path / TMP_DIR).iterdir()) == []  # deleted afterwards


async def test_read_photo_ai_error_deletes_the_photo_too(hass: HomeAssistant, ws, tmp_path: Path) -> None:
    _ai_entity(hass)

    async def generate_data(call: ServiceCall) -> dict:
        raise HomeAssistantError("provider down")

    hass.services.async_register("ai_task", "generate_data", generate_data, supports_response=SupportsResponse.ONLY)
    result = await _call(ws, 1, type=f"{DOMAIN}/read_photo", image=PHOTO_B64)
    assert result["status"] == "error"
    assert list((tmp_path / TMP_DIR).iterdir()) == []


async def test_read_photo_rejects_what_is_not_an_image(hass: HomeAssistant, ws) -> None:
    _ai_entity(hass)
    assert (await _call(ws, 1, type=f"{DOMAIN}/read_photo", image="%%%"))["status"] == "error"


async def test_parse_photo_text(ws) -> None:
    result = await _call(ws, 1, type=f"{DOMAIN}/parse_photo_text", text=SCREEN_TEXT)
    assert result == {
        "status": "ok",
        "source": "ocr",
        "fields": {
            "host": "192.168.1.123",
            "printer_serial": "0309AB123456789",
            "access_code": "4f7a9c2e",
            "printer_name": "Bambu Lab A1 mini",
        },
        "warnings": [],
    }
    assert (await _call(ws, 2, type=f"{DOMAIN}/parse_photo_text", text="nothing here"))["status"] == "empty"
