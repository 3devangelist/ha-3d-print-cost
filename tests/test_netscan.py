"""Finding Moonraker on the network without zeroconf (pure helpers + probing with a fake client)."""

import asyncio
from typing import ClassVar
from unittest.mock import patch

from custom_components.fdm_printer_manager.adapters.moonraker import MoonrakerAuthError, MoonrakerConnectionError
from custom_components.fdm_printer_manager.adapters.netscan import (
    SCAN_PORTS,
    FoundKlipper,
    async_probe,
    async_scan,
    subnet_hosts,
    unique_hosts,
)

CLIENT = "custom_components.fdm_printer_manager.adapters.netscan.MoonrakerClient"


def test_ports_are_moonraker_default_then_80() -> None:
    assert SCAN_PORTS == (7125, 80)


def test_subnet_hosts_of_a_24() -> None:
    hosts = subnet_hosts("192.168.1.10", 24)
    assert len(hosts) == 253  # .1 to .254 without Home Assistant itself
    assert hosts[0] == "192.168.1.1"
    assert hosts[-1] == "192.168.1.254"
    assert "192.168.1.10" not in hosts


def test_wide_networks_are_cut_to_a_24() -> None:
    hosts = subnet_hosts("10.0.5.20", 16)
    assert len(hosts) == 253
    assert all(h.startswith("10.0.5.") for h in hosts)


def test_public_loopback_and_link_local_are_never_scanned() -> None:
    assert subnet_hosts("8.8.8.8", 24) == []
    assert subnet_hosts("127.0.0.1", 8) == []
    assert subnet_hosts("169.254.3.4", 16) == []
    assert subnet_hosts("not an ip", 24) == []
    assert subnet_hosts("fe80::1", 64) == []


def test_unique_hosts_keeps_order() -> None:
    assert unique_hosts([["a", "b"], ["b", "c"], []]) == ["a", "b", "c"]


class FakeClient:
    """Answers like Moonraker only on the addresses in ``ANSWERS`` ({(host, port): ok | 'auth'})."""

    ANSWERS: ClassVar[dict[tuple[str, int], str]] = {}
    asked: ClassVar[list[tuple[str, int]]] = []

    def __init__(self, _session, host, port=7125, api_key=None, *, timeout=10) -> None:
        self.key = (host, int(port))

    async def server_info(self):
        FakeClient.asked.append(self.key)
        answer = self.ANSWERS.get(self.key)
        if answer == "auth":
            raise MoonrakerAuthError("401")
        if answer is None:
            raise MoonrakerConnectionError("timeout")
        return {"klippy_state": "ready"}


def _fake(answers: dict[tuple[str, int], str]):
    FakeClient.ANSWERS = answers
    FakeClient.asked = []
    return patch(CLIENT, FakeClient)


async def test_probe_tries_7125_then_80() -> None:
    with _fake({("192.168.1.95", 80): "ok"}):
        found = await async_probe(None, "192.168.1.95")
    assert found == FoundKlipper("192.168.1.95", 80)
    assert FakeClient.asked == [("192.168.1.95", 7125), ("192.168.1.95", 80)]


async def test_probe_stops_at_the_first_port_that_answers() -> None:
    with _fake({("192.168.1.89", 7125): "ok", ("192.168.1.89", 80): "ok"}):
        found = await async_probe(None, "192.168.1.89")
    assert found == FoundKlipper("192.168.1.89", 7125)
    assert FakeClient.asked == [("192.168.1.89", 7125)]


async def test_probe_counts_a_key_request_as_found() -> None:
    with _fake({("192.168.1.89", 7125): "auth"}):
        found = await async_probe(None, "192.168.1.89")
    assert found == FoundKlipper("192.168.1.89", 7125, needs_api_key=True)


async def test_probe_nothing_answers() -> None:
    with _fake({}):
        assert await async_probe(None, "192.168.1.50") is None


async def test_scan_returns_the_printers_in_host_order() -> None:
    hosts = [f"192.168.1.{n}" for n in range(1, 101)]
    answers = {("192.168.1.95", 80): "ok", ("192.168.1.89", 7125): "ok"}
    with _fake(answers):
        found = await async_scan(None, hosts, concurrency=8)
    assert found == [FoundKlipper("192.168.1.89", 7125), FoundKlipper("192.168.1.95", 80)]
    assert len({h for h, _ in FakeClient.asked}) == 100  # every address asked once per port at most


async def test_scan_never_has_more_requests_open_than_the_limit() -> None:
    state = {"open": 0, "max": 0}

    class SlowClient(FakeClient):
        async def server_info(self):
            state["open"] += 1
            state["max"] = max(state["max"], state["open"])
            await asyncio.sleep(0.001)
            state["open"] -= 1
            raise MoonrakerConnectionError("timeout")

    with patch(CLIENT, SlowClient):
        assert await async_scan(None, [f"10.0.0.{n}" for n in range(1, 60)], concurrency=5) == []
    assert state["max"] == 5
