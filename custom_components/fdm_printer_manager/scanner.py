"""Look for Klipper printers on Home Assistant's network and offer them in "Found on the network".

Runs when the integration starts, every 30 minutes, and on demand (the panel's
"Search again" and "Add printer"). Each printer found starts a discovery flow,
which Home Assistant keeps once per printer (same unique id as a printer added
by hand, so configured printers are never offered again).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.components import network
from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started

from .adapters.moonraker import MoonrakerClient, MoonrakerError
from .adapters.netscan import SCAN_TIMEOUT, FoundKlipper, async_scan, subnet_hosts, unique_hosts
from .const import CONF_HOST, CONF_PORT, CONF_PRINTER_NAME, CONF_PRINTER_TYPE, DOMAIN, PrinterType

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=30)


async def async_local_hosts(hass: HomeAssistant) -> list[str]:
    """Addresses of the private IPv4 subnets Home Assistant is on (at most a /24 each)."""
    adapters = await network.async_get_adapters(hass)
    return unique_hosts(
        subnet_hosts(ip["address"], ip["network_prefix"])
        for adapter in adapters
        if adapter.get("enabled")
        for ip in adapter.get("ipv4") or []
    )


async def async_scan_network(hass: HomeAssistant) -> list[FoundKlipper]:
    """Scan now (one scan at a time) and offer every Klipper printer found."""
    lock: asyncio.Lock = hass.data.setdefault(DOMAIN, {}).setdefault("scan_lock", asyncio.Lock())
    async with lock:
        hosts = await async_local_hosts(hass)
        if not hosts:
            _LOGGER.debug("Klipper scan skipped: no private IPv4 network")
            return []
        session = async_get_clientsession(hass)
        found = await async_scan(session, hosts)
        for printer in found:
            await _async_offer(hass, printer)
        return found


async def _async_offer(hass: HomeAssistant, printer: FoundKlipper) -> None:
    """Start a discovery flow for one printer (Home Assistant drops duplicates)."""
    name = None
    if not printer.needs_api_key:
        client = MoonrakerClient(async_get_clientsession(hass), printer.host, printer.port, timeout=SCAN_TIMEOUT * 2)
        try:
            name = await client.hostname()
        except MoonrakerError:
            name = None
    await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_INTEGRATION_DISCOVERY},
        data={
            CONF_PRINTER_TYPE: PrinterType.KLIPPER,
            CONF_HOST: printer.host,
            CONF_PORT: printer.port,
            CONF_PRINTER_NAME: name,
        },
    )


@callback
def async_start_scanning(hass: HomeAssistant) -> CALLBACK_TYPE:
    """First scan once Home Assistant has started, then every 30 minutes. Returns the stop function."""

    async def _scan(*_args: object) -> None:
        try:
            await async_scan_network(hass)
        except Exception:  # a failed scan must never break the integration
            _LOGGER.exception("Klipper network scan failed")

    @callback
    def _start(_hass: HomeAssistant) -> None:
        hass.async_create_background_task(_scan(), f"{DOMAIN} klipper scan")

    stop_at_started = async_at_started(hass, _start)
    stop_interval = async_track_time_interval(hass, _scan, SCAN_INTERVAL, name=f"{DOMAIN} klipper scan")

    @callback
    def _stop() -> None:
        stop_at_started()
        stop_interval()

    return _stop
