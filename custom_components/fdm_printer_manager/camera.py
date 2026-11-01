"""``camera.<printer>_camera``: the printer's own camera, when one is found.

Klipper: the first enabled webcam Moonraker lists (crowsnest, mjpg-streamer...).
Nothing to configure: the entity is created the first time a camera is found
and goes unavailable if the camera disappears. Bambu Lab printers set up
through ha-bambulab already have a camera entity; the panel shows that one
instead of creating a second.
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

from .const import DOMAIN, HTTP_TIMEOUT
from .coordinator import PrintCostConfigEntry, PrintCostCoordinator
from .entity import PrintCostEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrintCostConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the camera once Moonraker reports one (right away if it was there before)."""
    coordinator = entry.runtime_data
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
        """One JPEG from the snapshot URL (redirects are followed)."""
        webcam = self.coordinator.webcam
        if webcam is None or not webcam.snapshot_url:
            return None
        try:
            async with self._session.get(
                webcam.snapshot_url, timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
            ) as resp:
                if resp.status >= 400:
                    _LOGGER.debug("%s: snapshot HTTP %s", self.coordinator.printer_name, resp.status)
                    return None
                return await resp.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("%s: snapshot failed: %s", self.coordinator.printer_name, err)
            return None

    async def handle_async_mjpeg_stream(self, request: web.Request) -> web.StreamResponse | None:
        """Relay the printer's MJPEG stream; without one, Home Assistant streams the snapshots."""
        webcam = self.coordinator.webcam
        if webcam is None or not webcam.stream_url:
            return await super().handle_async_mjpeg_stream(request)
        stream = self._session.get(
            webcam.stream_url, timeout=aiohttp.ClientTimeout(total=None, sock_connect=HTTP_TIMEOUT)
        )
        return await async_aiohttp_proxy_web(self.hass, request, stream)
