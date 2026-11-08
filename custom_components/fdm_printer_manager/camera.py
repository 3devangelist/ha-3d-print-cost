"""``camera.<printer>_camera``: the printer's own camera, when one is found.

Klipper: the first enabled webcam Moonraker lists (crowsnest, mjpg-streamer...).
Nothing to configure: the entity is created the first time a camera is found
and goes unavailable if the camera disappears.

Bambu Lab in LAN mode (no other integration): A1, A1 mini, P1P and P1S send
JPEGs on port 6000 (adapters/bambu_camera.py); the connection is open only
while someone is watching. X1 / H2 (RTSP on port 322) are not supported yet.

Bambu Lab printers set up through ha-bambulab already have a camera entity;
the panel shows that one instead of creating a second.
"""

from __future__ import annotations

import logging

import aiohttp
from aiohttp import web
from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_aiohttp_proxy_web, async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .adapters.moonraker import read_limited
from .const import DOMAIN, HTTP_TIMEOUT
from .coordinator import PrintCostConfigEntry, PrintCostCoordinator
from .entity import PrintCostEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# A snapshot is one JPEG: anything bigger is refused.
MAX_SNAPSHOT = 10 * 1024 * 1024


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrintCostConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the camera once Moonraker reports one (right away if it was there before); Bambu LAN: right away."""
    coordinator = entry.runtime_data
    if coordinator.bambu_camera is not None:
        async_add_entities([BambuLanCamera(coordinator)])
        return
    if coordinator.klipper is None:
        return
    known = er.async_get(hass).async_get_entity_id("camera", DOMAIN, f"{entry.entry_id}_camera")
    added = False

    @callback
    def _add_when_found() -> None:
        nonlocal added
        if added or (coordinator.webcam is None and known is None):
            return
        added = True
        async_add_entities([PrinterCamera(coordinator)])

    _add_when_found()
    entry.async_on_unload(coordinator.async_add_listener(_add_when_found))


class PrinterCamera(PrintCostEntity, Camera):
    """Snapshots and the live MJPEG stream of a Klipper printer's webcam."""

    def __init__(self, coordinator: PrintCostCoordinator) -> None:
        PrintCostEntity.__init__(self, coordinator, "camera", "camera")
        Camera.__init__(self)
        self._session = async_get_clientsession(coordinator.hass)

    async def async_added_to_hass(self) -> None:
        """Let the printer status sensor name this camera right away (the panel reads it there)."""
        await super().async_added_to_hass()
        self.coordinator.async_update_listeners()

    @property
    def available(self) -> bool:
        """Only while the printer answers and still lists a camera."""
        klipper = self.coordinator.klipper
        return self.coordinator.webcam is not None and klipper is not None and klipper.available

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        """One JPEG from the snapshot URL: same host as the printer, no redirects, at most MAX_SNAPSHOT bytes."""
        webcam = self.coordinator.webcam
        if webcam is None or not webcam.snapshot_url:
            return None
        try:
            async with self._session.get(
                webcam.snapshot_url, timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT), allow_redirects=False
            ) as resp:
                if resp.status >= 300:
                    _LOGGER.debug("%s: snapshot HTTP %s", self.coordinator.printer_name, resp.status)
                    return None
                return await read_limited(resp, MAX_SNAPSHOT)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("%s: snapshot failed: %s", self.coordinator.printer_name, err)
            return None

    async def handle_async_mjpeg_stream(self, request: web.Request) -> web.StreamResponse | None:
        """Relay the printer's MJPEG stream; without one, Home Assistant streams the snapshots."""
        webcam = self.coordinator.webcam
        if webcam is None or not webcam.stream_url:
            return await super().handle_async_mjpeg_stream(request)
        stream = self._session.get(
            webcam.stream_url,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=HTTP_TIMEOUT, sock_read=HTTP_TIMEOUT * 3),
            allow_redirects=False,
        )
        return await async_aiohttp_proxy_web(self.hass, request, stream)


class BambuLanCamera(PrintCostEntity, Camera):
    """Images of a Bambu Lab A1 / P1 in LAN mode (port 6000), live as a stream of stills."""

    # The printer sends one JPEG every 1-2 seconds: asking more often only repeats the same one.
    _attr_frame_interval = 1.0

    def __init__(self, coordinator: PrintCostCoordinator) -> None:
        PrintCostEntity.__init__(self, coordinator, "camera", "camera")
        Camera.__init__(self)

    async def async_added_to_hass(self) -> None:
        """Let the printer status sensor name this camera right away (the panel reads it there)."""
        await super().async_added_to_hass()
        self.coordinator.async_update_listeners()

    @property
    def available(self) -> bool:
        """Only while the printer answers over MQTT and is still the known printer."""
        bambu_lan = self.coordinator.bambu_lan
        return bambu_lan is not None and bambu_lan.available and not self.coordinator.identity.changed

    @property
    def is_streaming(self) -> bool:
        """The connection to the camera is open."""
        camera = self.coordinator.bambu_camera
        return camera is not None and camera.connected

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        """The latest JPEG from the printer (``None`` when it is off, refuses or sends nothing)."""
        camera = self.coordinator.bambu_camera
        if camera is None or not self.available:
            return None
        return await camera.async_image()
