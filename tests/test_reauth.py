"""Bambu Lab LAN: the access code changed, Home Assistant asks for the new one."""

import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fdm_printer_manager.adapters.bambu_lan import (
    BambuAuthError,
    BambuLanClient,
    BambuStatus,
)
from custom_components.fdm_printer_manager.const import DOMAIN

TEST = "custom_components.fdm_printer_manager.config_flow.BambuLanClient.async_test"
SERIAL = "0309DA561105372"


def _entry(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="A1 mini",
        unique_id=f"bambu_{SERIAL}",
        data={
            "printer_type": "bambu_lan",
            "printer_name": "A1 mini",
            "host": "192.168.1.96",
            "printer_serial": SERIAL,
            "access_code": "oldcode1",
        },
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


async def test_reauth_updates_the_code(hass: HomeAssistant) -> None:
    entry = _entry(hass, options={"access_code": "oldcode2", "update_interval": 30})
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    with patch(TEST, AsyncMock(side_effect=BambuAuthError("no"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.96", "access_code": "wrong"}
        )
    assert result["errors"] == {"access_code": "invalid_auth"}

    with (
        patch(TEST, AsyncMock(return_value=BambuStatus("idle"))),
        patch("custom_components.fdm_printer_manager.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.97", "access_code": "newcode9"}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    merged = {**entry.data, **entry.options}
    assert merged["access_code"] == "newcode9"
    assert merged["host"] == "192.168.1.97"
    assert entry.options["update_interval"] == 30


async def test_refused_code_calls_back_once() -> None:
    """The client tells the coordinator once per refusal streak (not every retry)."""
    calls: list[int] = []
    client = BambuLanClient("127.0.0.1", SERIAL, "x", lambda: None, on_auth_failed=lambda: calls.append(1))
    opens = 0

    async def refuse():
        nonlocal opens
        opens += 1
        if opens > 2:
            raise asyncio.CancelledError
        raise BambuAuthError("refused")

    with (
        patch.object(client, "_open", side_effect=refuse),
        patch("custom_components.fdm_printer_manager.adapters.bambu_lan.asyncio.sleep", AsyncMock()),
        contextlib.suppress(asyncio.CancelledError),
    ):
        await client._run()
    assert calls == [1]
    assert client.auth_failed


async def test_refused_code_starts_reauth(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    with patch("custom_components.fdm_printer_manager.coordinator.BambuLanClient.start"):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    entry.runtime_data._on_bambu_auth_failed()
    await hass.async_block_till_done()
    flows = [f for f in hass.config_entries.flow.async_progress() if f["context"]["source"] == SOURCE_REAUTH]
    assert len(flows) == 1
    assert flows[0]["context"]["entry_id"] == entry.entry_id
