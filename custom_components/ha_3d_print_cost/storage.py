"""Persistence through Home Assistant's ``Store`` (``.storage/ha_3d_print_cost.<entry_id>``).

The thumbnail of the last print is kept next to it, in its own file
(``.storage/ha_3d_print_cost.<entry_id>.image``): the JSON is rewritten every
few seconds while printing and must stay small.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import STORAGE_DIR, Store

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


class JobStore:
    """Thin wrapper around ``Store`` with (de)serialisation."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._image_path = Path(hass.config.path(STORAGE_DIR, f"{STORAGE_KEY_PREFIX}.{entry_id}.image"))
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}.{entry_id}", atomic_writes=True
        )

    async def async_load(self) -> StoredState:
        """Load, tolerating corrupted entries (they are dropped, not fatal)."""
        raw = await self._store.async_load()
        if not raw:
            return StoredState()
        state = StoredState()
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
        """Delete the files (entry removed)."""
        await self._store.async_remove()
        await self.async_save_image(None)

    async def async_load_image(self) -> bytes | None:
        """Thumbnail of the last print, if one was saved."""
        return await self._hass.async_add_executor_job(_read_bytes, self._image_path)

    async def async_save_image(self, content: bytes | None) -> None:
        """Replace the thumbnail of the last print (``None`` deletes it)."""
        await self._hass.async_add_executor_job(_write_bytes, self._image_path, content)


def _serialise(state: StoredState) -> dict[str, Any]:
    return {
        "active_job": state.active_job.as_dict() if state.active_job else None,
        "finalizing": [j.as_dict() for j in state.finalizing],
        "last_job": state.last_job.as_dict() if state.last_job else None,
    }


def _job_or_none(data: Any) -> PrintJob | None:
    if not isinstance(data, dict):
        return None
    try:
        return PrintJob.from_dict(data)
    except (KeyError, TypeError, ValueError) as err:
        _LOGGER.error("Dropping unreadable stored print job: %s", err)
        return None


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as err:
        _LOGGER.warning("Cannot read the last print image: %s", err)
        return None


def _write_bytes(path: Path, content: bytes | None) -> None:
    try:
        if content is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_bytes(content)
        tmp.replace(path)
    except OSError as err:
        _LOGGER.warning("Cannot write the last print image: %s", err)
