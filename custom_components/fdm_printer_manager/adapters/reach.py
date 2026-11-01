"""Check whether the printer's computer is still running.

Used after asking Moonraker to shut the host down: the plug is cut only once
the computer stops answering. Moonraker itself stops early in the shutdown, so
"Moonraker does not answer" is not enough; the operating system has to be gone.

* ICMP ping (``icmplib``, unprivileged) when it is installed and allowed;
* then a TCP connection to the Moonraker port: an answer *or a refusal*
  means the computer is up (a refusal comes from its kernel), only a timeout
  or "no route" means it is down.
"""

from __future__ import annotations

import asyncio
import logging

_LOGGER = logging.getLogger(__name__)

REACH_TIMEOUT = 1.5  # seconds

# None: not tried yet; False: icmplib missing or not allowed here, use TCP.
_icmp_works: bool | None = None


async def _icmp_alive(host: str) -> bool | None:
    """Ping once; ``None`` when ICMP cannot be used here."""
    global _icmp_works  # noqa: PLW0603 - one answer for the whole process
    if _icmp_works is False:
        return None
    try:
        from icmplib import async_ping  # noqa: PLC0415 - optional, comes with HA's ping integration

        result = await async_ping(host, count=1, timeout=REACH_TIMEOUT, privileged=False)
    except Exception as err:  # noqa: BLE001 - ImportError, SocketPermissionError, ...
        _LOGGER.debug("ICMP ping not usable (%s): falling back to TCP", err)
        _icmp_works = False
        return None
    _icmp_works = True
    return bool(result.is_alive)


async def _tcp_alive(host: str, port: int) -> bool:
    """Connected or refused: the computer is up. Timeout or unreachable: down."""
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), REACH_TIMEOUT)
    except ConnectionRefusedError:
        return True
    except (TimeoutError, OSError):
        return False
    writer.close()
    return True


async def host_is_up(host: str, port: int) -> bool:
    """Whether the computer at ``host`` still answers.

    Down only when neither the ping nor the TCP check gets an answer, so a
    printer that drops pings is never taken for switched off.
    """
    if await _icmp_alive(host):
        return True
    return await _tcp_alive(host, port)
