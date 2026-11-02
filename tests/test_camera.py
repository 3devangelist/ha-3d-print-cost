"""The printer camera: found by itself, nothing to configure."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.camera import async_get_image
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.fdm_printer_manager.adapters.moonraker import (
    KlipperStatus,
    MoonrakerClient,
    MoonrakerConnectionError,
    Webcam,
    parse_webcams,
)
from custom_components.fdm_printer_manager.const import DOMAIN, WEBCAM_CHECK_INTERVAL

from .helpers import SERIAL
from .test_integration import make_entry

CLIENT = "custom_components.fdm_printer_manager.coordinator.MoonrakerClient"
CENTAURI_CAMS = {
    "webcams": [
        {
            "name": "Centauri",
            "enabled": True,
            "service": "mjpegstreamer",
            "stream_url": "/webcam/?action=stream",
            "snapshot_url": "/webcam/?action=snapshot",
        }
    ]
}
SNAPSHOT = "http://192.168.1.95/webcam/?action=snapshot"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_relative_urls_follow_the_web_interface() -> None:
    """Relative URLs point at the web server (port 80), not at Moonraker's port."""
    cams = parse_webcams(CENTAURI_CAMS, "http://192.168.1.95/")
    assert cams == [
        Webcam(
            name="Centauri",
            snapshot_url=SNAPSHOT,
            stream_url="http://192.168.1.95/webcam/?action=stream",
        )
    ]


def test_absolute_urls_are_kept() -> None:
    cams = parse_webcams(
        {"webcams": [{"name": "cam", "snapshot_url": "http://192.168.1.95:8080/?action=snapshot"}]},
        "http://192.168.1.89/",
    )
    assert cams[0].snapshot_url == "http://192.168.1.95:8080/?action=snapshot"
    assert cams[0].stream_url is None


def test_disabled_and_empty_cameras_are_skipped() -> None:
    result = {
        "webcams": [
            {"name": "off", "enabled": False, "snapshot_url": "/a"},
            {"name": "nothing", "enabled": True},
        ]
    }
    assert parse_webcams(result, "http://h/") == []
    assert parse_webcams({"webcams": []}, "http://h/") == []


def test_only_mjpeg_streams_are_relayed() -> None:
    """WebRTC / HLS streams cannot go in an <img>: snapshots only."""
    result = {"webcams": [{"name": "c", "service": "webrtc-camerastreamer", "stream_url": "/s", "snapshot_url": "/p"}]}
    cam = parse_webcams(result, "http://h/")[0]
    assert cam.stream_url is None
    assert cam.snapshot_url == "http://h/p"


async def test_old_moonraker_without_webcam_api_has_no_camera() -> None:
    client = MoonrakerClient(AsyncMock(), "192.168.1.89")
    with patch.object(client, "_get", AsyncMock(side_effect=MoonrakerConnectionError("HTTP 404 from x"))):
        assert await client.webcams("http://192.168.1.89/") == []
    with (
        patch.object(client, "_get", AsyncMock(side_effect=MoonrakerConnectionError("timeout"))),
        pytest.raises(MoonrakerConnectionError),
    ):
        await client.webcams("http://192.168.1.89/")


# --------------------------------------------------------------------------- #
# Klipper camera entity
# --------------------------------------------------------------------------- #
class FakeCentauri:
    """Moonraker answers; tests change ``cams`` between checks."""

    def __init__(self) -> None:
        self.cams: list[Webcam] | Exception = []
        self.reading: KlipperStatus | Exception = KlipperStatus(state="standby", nozzle_temp=25)

    async def status(self) -> KlipperStatus:
        if isinstance(self.reading, Exception):
            raise self.reading
        return self.reading

    async def webcams(self, web_url: str, hosts: frozenset[str] | None = None) -> list[Webcam]:
        if isinstance(self.cams, Exception):
            raise self.cams
        return self.cams


@pytest.fixture
def centauri():
    fake = FakeCentauri()
    with patch.multiple(
        CLIENT,
        status=AsyncMock(side_effect=fake.status),
        metadata=AsyncMock(side_effect=MoonrakerConnectionError("none")),
        webcams=AsyncMock(side_effect=fake.webcams),
    ):
        yield fake


async def setup_centauri(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Centauri",
        unique_id="klipper_192.168.1.95_80",
        data={"printer_type": "klipper", "printer_name": "Centauri", "host": "192.168.1.95", "port": 80},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_camera_found_at_start(
    hass: HomeAssistant, centauri: FakeCentauri, aioclient_mock: AiohttpClientMocker
) -> None:
    centauri.cams = parse_webcams(CENTAURI_CAMS, "http://192.168.1.95/")
    aioclient_mock.get(SNAPSHOT, content=JPEG)
    await setup_centauri(hass)

    state = hass.states.get("camera.centauri_camera")
    assert state is not None
    assert state.state != "unavailable"
    status = hass.states.get("sensor.centauri_printer_status")
    assert status.attributes["camera_entity"] == "camera.centauri_camera"

    image = await async_get_image(hass, "camera.centauri_camera")
    assert image.content == JPEG
    assert str(aioclient_mock.mock_calls[-1][1]) == SNAPSHOT


async def test_no_camera_no_entity(hass: HomeAssistant, centauri: FakeCentauri) -> None:
    await setup_centauri(hass)
    assert hass.states.get("camera.centauri_camera") is None
    assert hass.states.get("sensor.centauri_printer_status").attributes["camera_entity"] is None


async def test_camera_installed_later_then_removed(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, centauri: FakeCentauri
) -> None:
    """A new camera shows up on the next check without reconfiguring; a removed one goes unavailable."""
    freezer.move_to("2026-11-01 10:00:00+00:00")
    await setup_centauri(hass)
    assert hass.states.get("camera.centauri_camera") is None

    centauri.cams = parse_webcams(CENTAURI_CAMS, "http://192.168.1.95/")
    await tick(hass, freezer, WEBCAM_CHECK_INTERVAL)
    assert hass.states.get("camera.centauri_camera").state != "unavailable"

    centauri.cams = []
    await tick(hass, freezer, WEBCAM_CHECK_INTERVAL)
    assert hass.states.get("camera.centauri_camera").state == "unavailable"
    assert hass.states.get("sensor.centauri_printer_status").attributes["camera_entity"] is None


async def test_failed_check_is_retried_on_the_next_poll(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, centauri: FakeCentauri
) -> None:
    """Printer off when HA starts: the camera is looked for as soon as it answers."""
    freezer.move_to("2026-11-01 10:00:00+00:00")
    centauri.reading = MoonrakerConnectionError("off")
    await setup_centauri(hass)
    assert hass.states.get("camera.centauri_camera") is None

    centauri.reading = KlipperStatus(state="standby", nozzle_temp=25)
    centauri.cams = parse_webcams(CENTAURI_CAMS, "http://192.168.1.95/")
    await tick(hass, freezer, 5)
    await tick(hass, freezer, 5)
    assert hass.states.get("camera.centauri_camera").state != "unavailable"


async def test_known_camera_is_kept_while_the_printer_is_off(hass: HomeAssistant, centauri: FakeCentauri) -> None:
    """After a restart with the printer off, the camera stays (unavailable) instead of vanishing."""
    er.async_get(hass).async_get_or_create(
        "camera", DOMAIN, "klipper-entry_camera", suggested_object_id="centauri_camera"
    )
    centauri.reading = MoonrakerConnectionError("off")
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="klipper-entry",
        title="Centauri",
        unique_id="klipper_192.168.1.95_80",
        data={"printer_type": "klipper", "printer_name": "Centauri", "host": "192.168.1.95", "port": 80},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("camera.centauri_camera").state == "unavailable"


# --------------------------------------------------------------------------- #
# Bambu Lab through ha-bambulab
# --------------------------------------------------------------------------- #
async def test_bambulab_camera_is_reused_not_duplicated(hass: HomeAssistant) -> None:
    entry = make_entry(hass)
    device_id = entry.data["printer_device_id"]
    bambu_entry = hass.config_entries.async_entries("bambu_lab")[0]
    er.async_get(hass).async_get_or_create(
        "camera",
        "bambu_lab",
        f"{SERIAL}_camera",
        suggested_object_id="x2d_camera",
        device_id=device_id,
        config_entry=bambu_entry,
        translation_key="camera",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    status = hass.states.get("sensor.x2d_printer_status")
    assert status.attributes["camera_entity"] == "camera.x2d_camera"
    ours = [e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id) if e.domain == "camera"]
    assert ours == []


async def test_bambulab_image_camera_of_a1_and_p1(hass: HomeAssistant) -> None:
    """A1 / P1: ha-bambulab shows the camera as an image entity that refreshes."""
    entry = make_entry(hass)
    device_id = entry.data["printer_device_id"]
    bambu_entry = hass.config_entries.async_entries("bambu_lab")[0]
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "image",
        "bambu_lab",
        f"{SERIAL}_cover_image",
        suggested_object_id="x2d_cover_image",
        device_id=device_id,
        config_entry=bambu_entry,
        translation_key="cover_image",
    )
    registry.async_get_or_create(
        "image",
        "bambu_lab",
        f"{SERIAL}_p1p_camera",
        suggested_object_id="x2d_camera",
        device_id=device_id,
        config_entry=bambu_entry,
        translation_key="p1p_camera",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.x2d_printer_status").attributes["camera_entity"] == "image.x2d_camera"
