"""Find Klipper printers on the local network without zeroconf.

Many Moonraker installs have no ``[zeroconf]`` section, so they never announce
themselves. This module asks every address of Home Assistant's own subnet
whether Moonraker answers ``/server/info``, on port 7125 (the default) and 80
(some printers, like the Elegoo Centauri Carbon, only serve it there).

Requests are capped (a few dozen at a time, short timeouts) and the subnet is
limited to a /24 around Home Assistant, so a scan never floods the network.
The address helpers are pure functions and unit-tested without a network.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from collections.abc import Iterable
from dataclasses import dataclass

import aiohttp

from .moonraker import MoonrakerAuthError, MoonrakerClient, MoonrakerError

_LOGGER = logging.getLogger(__name__)

SCAN_PORTS: tuple[int, ...] = (7125, 80)
SCAN_TIMEOUT = 1.5  # seconds per request
SCAN_CONCURRENCY = 32
MIN_PREFIX = 24  # never scan more than 254 addresses per interface


@dataclass(frozen=True, slots=True)
class FoundKlipper:
    """A Moonraker server that answered."""

    host: str
    port: int
    needs_api_key: bool = False


def subnet_hosts(address: str, prefix: int) -> list[str]:
    """Addresses to ask around ``address`` (itself excluded), at most a /24.

    Anything that is not a private IPv4 network is skipped: no scanning of
    public or link-local ranges.
    """
    try:
        ip = ipaddress.IPv4Address(address)
    except ValueError:
        return []
    if not ip.is_private or ip.is_loopback or ip.is_link_local:
        return []
    network = ipaddress.IPv4Network(f"{ip}/{max(int(prefix), MIN_PREFIX)}", strict=False)
    return [str(host) for host in network.hosts() if host != ip]


def unique_hosts(groups: Iterable[Iterable[str]]) -> list[str]:
    """Merge the host lists of several interfaces, keeping the first order."""
    seen: dict[str, None] = {}
    for group in groups:
        for host in group:
            seen.setdefault(host, None)
    return list(seen)


async def async_probe(
    session: aiohttp.ClientSession,
    host: str,
    ports: Iterable[int] = SCAN_PORTS,
    *,
    timeout: float = SCAN_TIMEOUT,
) -> FoundKlipper | None:
    """First port (in order) where Moonraker answers. A key request still counts as found."""
    for port in ports:
        client = MoonrakerClient(session, host, port, timeout=timeout)
        try:
            await client.server_info()
        except MoonrakerAuthError:
            return FoundKlipper(host, int(port), needs_api_key=True)
        except MoonrakerError:
            continue
        return FoundKlipper(host, int(port))
    return None


async def async_scan(
    session: aiohttp.ClientSession,
    hosts: Iterable[str],
    ports: Iterable[int] = SCAN_PORTS,
    *,
    concurrency: int = SCAN_CONCURRENCY,
    timeout: float = SCAN_TIMEOUT,
) -> list[FoundKlipper]:
    """Probe ``hosts`` a few at a time; return the Moonraker servers found, in host order."""
    ports = tuple(ports)
    limit = asyncio.Semaphore(concurrency)

    async def one(host: str) -> FoundKlipper | None:
        async with limit:
            return await async_probe(session, host, ports, timeout=timeout)

    hosts = list(hosts)
    results = await asyncio.gather(*(one(host) for host in hosts))
    found = [r for r in results if r is not None]
    _LOGGER.debug("Klipper scan: %d addresses, %d Moonraker found", len(hosts), len(found))
    return found
