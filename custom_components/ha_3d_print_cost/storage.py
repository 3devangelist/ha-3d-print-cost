"""Persistence through Home Assistant's ``Store`` (``.storage/ha_3d_print_cost.<entry_id>``)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY_PREFIX, STORAGE_VERSION
from .models import PrintJob

_LOGGER = logging.getLogger(__name__)

SAVE_DELAY = 10  # seconds, for frequent updates while printing


@dataclass(slots=True)
class StoredState:
    """Everything that must survive a restart."""

    active_job: PrintJob | None = None
    finalizing: list[PrintJob] = field(default_factory=list)
    last_job: PrintJob | None = None
    history: list[dict[str, Any]] = field(default_factory=list)  # newest first
    last_estimate: dict[str, Any] | None = None  # last estimate_cost result


class JobStore:
    """Thin wrapper around ``Store`` with (de)serialisation."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}.{entry_id}", atomic_writes=True
        )

    async def async_load(self) -> StoredState:
        """Load, tolerating corrupted entries (they are dropped, not fatal)."""
        raw = await self._store.async_load()
        if not raw:
            return StoredState()
        state = StoredState(history=list(raw.get("history") or []))
        if isinstance(raw.get("last_estimate"), dict):
            state.last_estimate = raw["last_estimate"]
        state.active_job = _job_or_none(raw.get("active_job"))
        state.last_job = _job_or_none(raw.get("last_job"))
        state.finalizing = [j for j in (_job_or_none(d) for d in raw.get("finalizing") or []) if j]
        return state

    async def async_save(self, state: StoredState) -> None:
        """Save now (start / end of a print, shutdown)."""
        await self._store.async_save(_serialise(state))

    def async_delay_save(self, state: StoredState) -> None:
        """Save soon (periodic accumulation)."""
        self._store.async_delay_save(lambda: _serialise(state), SAVE_DELAY)

    async def async_remove(self) -> None:
        """Delete the file (entry removed)."""
        await self._store.async_remove()


def _serialise(state: StoredState) -> dict[str, Any]:
    return {
        "active_job": state.active_job.as_dict() if state.active_job else None,
        "finalizing": [j.as_dict() for j in state.finalizing],
        "last_job": state.last_job.as_dict() if state.last_job else None,
        "history": state.history,
        "last_estimate": state.last_estimate,
    }


def _job_or_none(data: Any) -> PrintJob | None:
    if not isinstance(data, dict):
        return None
    try:
        return PrintJob.from_dict(data)
    except (KeyError, TypeError, ValueError) as err:
        _LOGGER.error("Dropping unreadable stored print job: %s", err)
        return None
