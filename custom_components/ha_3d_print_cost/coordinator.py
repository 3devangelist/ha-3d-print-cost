"""Glue between Home Assistant and the pure business logic.

* listens to the Bambu, power, energy and price entities (push, no polling);
* also samples on a timer (default 30 s) so long flat periods are integrated;
* feeds :class:`PrintTracker`, persists, finalises jobs, fires the event.

Business rules live in ``tracker.py``, ``filament.py`` and ``models/``.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .adapters import bambu
from .adapters.electricity import ElectricityReader, ElectricitySources
from .adapters.haspelsync import HaspelsyncClient
from .adapters.spoolman import SpoolmanClient
from .const import (
    BAMBU_ENTITY_KEYS,
    CONF_CURRENCY,
    CONF_ENERGY_ENTITY,
    CONF_FILAMENT_FALLBACK,
    CONF_HASPELSYNC_API_KEY,
    CONF_HASPELSYNC_PRINTER_ID,
    CONF_HASPELSYNC_URL,
    CONF_HISTORY_SIZE,
    CONF_INTEGRATION_METHOD,
    CONF_JOB_NAME_ENTITY,
    CONF_POWER_ENTITY,
    CONF_PRICE_ENTITY,
    CONF_PRINT_STATUS_ENTITY,
    CONF_PRINT_WEIGHT_ENTITY,
    CONF_PRINTER_NAME,
    CONF_PRINTER_SERIAL,
    CONF_PROGRESS_ENTITY,
    CONF_REMAINING_TIME_ENTITY,
    CONF_SPOOLMAN_URL,
    CONF_UPDATE_INTERVAL,
    DEFAULT_FILAMENT_FALLBACK,
    DEFAULT_HISTORY_SIZE,
    DEFAULT_INTEGRATION_METHOD,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    EVENT_PRINT_FINISHED,
    FILAMENT_RESOLVE_ATTEMPTS,
    FILAMENT_RESOLVE_DELAY,
    HTTP_TIMEOUT,
    MAX_SAMPLE_GAP,
    FilamentFallback,
    IntegrationMethod,
)
from .filament import FilamentResolver, apply_outcome
from .models import FilamentStatus, JobPhase, PrintJob
from .storage import JobStore, StoredState
from .tracker import PrinterSnapshot, PrintTracker, TransitionKind

_LOGGER = logging.getLogger(__name__)

type PrintCostConfigEntry = ConfigEntry[PrintCostCoordinator]


@dataclass(slots=True)
class PrintCostData:
    """What entities read after each update."""

    now: datetime
    active_job: PrintJob | None
    finalizing_job: PrintJob | None
    last_job: PrintJob | None
    price_per_kwh: Decimal | None

    @property
    def current_job(self) -> PrintJob | None:
        """The job shown by the ``current_print_*`` sensors."""
        return self.active_job or self.finalizing_job


class PrintCostCoordinator(DataUpdateCoordinator[PrintCostData]):
    """One coordinator per configured printer."""

    config_entry: PrintCostConfigEntry

    def __init__(self, hass: HomeAssistant, entry: PrintCostConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} {entry.title}", update_interval=None)
        self.conf: dict[str, Any] = {**entry.data, **entry.options}
        self.printer_name: str = self.conf.get(CONF_PRINTER_NAME) or entry.title
        self.printer_serial: str | None = self.conf.get(CONF_PRINTER_SERIAL)
        self.currency: str = self.conf.get(CONF_CURRENCY) or hass.config.currency
        self.history_size: int = int(self.conf.get(CONF_HISTORY_SIZE, DEFAULT_HISTORY_SIZE))

        self.store = JobStore(hass, entry.entry_id)
        self.state = StoredState()
        self.tracker = PrintTracker(
            entry.entry_id,
            method=IntegrationMethod(self.conf.get(CONF_INTEGRATION_METHOD, DEFAULT_INTEGRATION_METHOD)),
            max_gap_s=MAX_SAMPLE_GAP,
        )
        self.reader = ElectricityReader(
            hass,
            ElectricitySources(
                power_entity=self.conf.get(CONF_POWER_ENTITY),
                energy_entity=self.conf.get(CONF_ENERGY_ENTITY),
                price_entity=self.conf.get(CONF_PRICE_ENTITY),
            ),
        )

        session = async_get_clientsession(hass)
        self.haspelsync: HaspelsyncClient | None = None
        if url := self.conf.get(CONF_HASPELSYNC_URL):
            self.haspelsync = HaspelsyncClient(
                session, url, self.conf.get(CONF_HASPELSYNC_API_KEY), timeout=HTTP_TIMEOUT
            )
        self.spoolman: SpoolmanClient | None = None
        if url := self.conf.get(CONF_SPOOLMAN_URL):
            self.spoolman = SpoolmanClient(session, url, timeout=HTTP_TIMEOUT)
        self.resolver = FilamentResolver(
            haspelsync=self.haspelsync,
            haspelsync_printer_id=self.haspelsync_printer_id,
            spoolman=self.spoolman,
            fallback=FilamentFallback(self.conf.get(CONF_FILAMENT_FALLBACK, DEFAULT_FILAMENT_FALLBACK)),
        )

        self.errors: deque[str] = deque(maxlen=20)
        self._unsubs: list[CALLBACK_TYPE] = []
        self._tasks: set[asyncio.Task] = set()
        self._finished_listeners: list[Callable[[dict[str, Any]], None]] = []

    # ------------------------------------------------------------------ #
    # Setup / teardown
    # ------------------------------------------------------------------ #
    async def async_start(self) -> None:
        """Load state, resume an interrupted print, subscribe to entities."""
        self.state = await self.store.async_load()
        self.tracker.restore(self.state.active_job)

        watched = [
            self.conf.get(k)
            for k in (*BAMBU_ENTITY_KEYS, CONF_POWER_ENTITY, CONF_ENERGY_ENTITY, CONF_PRICE_ENTITY)
            if self.conf.get(k)
        ]
        self._unsubs.append(async_track_state_change_event(self.hass, watched, self._on_state_change))
        interval = timedelta(seconds=int(self.conf.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)))
        self._unsubs.append(async_track_time_interval(self.hass, self._on_tick, interval))

        # Jobs that were waiting for filament data when HA stopped.
        for job in list(self.state.finalizing):
            self._spawn(self._async_finalize(job))

        self._process(dt_util.utcnow())

    async def async_stop(self) -> None:
        """Unsubscribe, cancel background work, flush to disk."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for task in list(self._tasks):
            task.cancel()
        self._sync_state()
        await self.store.async_save(self.state)

    def _spawn(self, coro) -> None:
        task = self.config_entry.async_create_background_task(self.hass, coro, f"{DOMAIN}_task")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ #
    # Event entity hook
    # ------------------------------------------------------------------ #
    @callback
    def async_add_finished_listener(self, listener: Callable[[dict[str, Any]], None]) -> CALLBACK_TYPE:
        """Register a callback for finished prints (used by the event entity)."""
        self._finished_listeners.append(listener)

        def _remove() -> None:
            self._finished_listeners.remove(listener)

        return _remove

    # ------------------------------------------------------------------ #
    # Sampling
    # ------------------------------------------------------------------ #
    @callback
    def _on_state_change(self, event: Event[EventStateChangedData]) -> None:
        self._process(dt_util.utcnow())

    @callback
    def _on_tick(self, now: datetime) -> None:
        self._process(dt_util.utcnow())

    def build_snapshot(self, now: datetime) -> PrinterSnapshot:
        """Read every configured entity."""
        hass = self.hass
        total_g, slots = bambu.read_print_weight(hass, self.conf.get(CONF_PRINT_WEIGHT_ENTITY))
        return PrinterSnapshot(
            timestamp=now,
            status=bambu.read_raw_status(hass, self.conf.get(CONF_PRINT_STATUS_ENTITY)),
            electricity=self.reader.sample(now),
            job_name=bambu.read_text(hass, self.conf.get(CONF_JOB_NAME_ENTITY)),
            progress=bambu.read_number(hass, self.conf.get(CONF_PROGRESS_ENTITY)),
            remaining_min=bambu.read_remaining_minutes(hass, self.conf.get(CONF_REMAINING_TIME_ENTITY)),
            planned_weight_g=total_g,
            planned_slot_weights=slots,
        )

    @callback
    def _process(self, now: datetime) -> None:
        snap = self.build_snapshot(now)
        transitions = self.tracker.process(snap)
        save_now = False
        for transition in transitions:
            job = transition.job
            if transition.kind is TransitionKind.STARTED:
                save_now = True
                if self.haspelsync is not None:
                    self._spawn(self._async_capture_slots(job))
            elif transition.kind is TransitionKind.ENDED:
                save_now = True
                self.state.finalizing.append(job)
                self._spawn(self._async_finalize(job))

        self._sync_state()
        if save_now:
            self._spawn(self.store.async_save(self.state))
        elif self.state.active_job is not None:
            self.store.async_delay_save(self.state)
        self._publish(now, snap.electricity.price_per_kwh)

    def _sync_state(self) -> None:
        self.state.active_job = self.tracker.active_job

    def _publish(self, now: datetime, price: Decimal | None) -> None:
        self.async_set_updated_data(
            PrintCostData(
                now=now,
                active_job=self.tracker.active_job,
                finalizing_job=self.state.finalizing[-1] if self.state.finalizing else None,
                last_job=self.state.last_job,
                price_per_kwh=price,
            )
        )

    # ------------------------------------------------------------------ #
    # Filament
    # ------------------------------------------------------------------ #
    @property
    def haspelsync_printer_id(self) -> str | None:
        """Printer id on the HaspelSync side (defaults to the Bambu serial)."""
        return self.conf.get(CONF_HASPELSYNC_PRINTER_ID) or self.printer_serial

    async def _async_capture_slots(self, job: PrintJob) -> None:
        """Snapshot slot -> spool at print start (used by the fallback)."""
        assert self.haspelsync is not None
        printer_id = self.haspelsync_printer_id
        if not printer_id:
            return
        try:
            slots = await self.haspelsync.get_current_slots(printer_id)
        except Exception as err:  # noqa: BLE001
            self._record_error(f"HaspelSync slots: {err}")
            return
        job.slot_spools = {label: s.spool_id for label, s in slots.items() if s.spool_id is not None}

    async def _async_finalize(self, job: PrintJob) -> None:
        """Resolve filament (with retries), complete the job, notify."""
        try:
            if not self.resolver.enabled:
                job.filament_status = FilamentStatus.DISABLED
            else:
                while True:
                    job.finalize_attempts += 1
                    last = job.finalize_attempts >= FILAMENT_RESOLVE_ATTEMPTS
                    outcome = await self.resolver.resolve(job, last_attempt=last)
                    if outcome.done:
                        apply_outcome(job, outcome)
                        break
                    self.store.async_delay_save(self.state)
                    await asyncio.sleep(FILAMENT_RESOLVE_DELAY)
        except asyncio.CancelledError:
            raise  # HA stopping: job stays FINALIZING in storage and resumes
        except Exception as err:  # noqa: BLE001 - never lose the electricity part
            _LOGGER.error("Could not resolve filament for '%s': %s", job.name, err)
            self._record_error(f"finalize: {err}")
            job.filament_status = FilamentStatus.UNAVAILABLE
            job.filament_note = f"error while resolving filament: {err}"

        job.phase = JobPhase.COMPLETED
        if job in self.state.finalizing:
            self.state.finalizing.remove(job)
        self.state.last_job = job
        summary = job.summary()
        self.state.history.insert(0, summary)
        del self.state.history[self.history_size :]
        await self.store.async_save(self.state)

        payload = {"printer": self.printer_name, "currency": self.currency, **summary}
        self.hass.bus.async_fire(EVENT_PRINT_FINISHED, {"entry_id": self.config_entry.entry_id, **payload})
        for listener in list(self._finished_listeners):
            listener(payload)
        _LOGGER.info(
            "Print '%s' finalised: result=%s total=%s %s",
            job.name,
            job.result,
            summary["total_cost"],
            self.currency,
        )
        self._publish(dt_util.utcnow(), self.reader.price_per_kwh())

    def _record_error(self, message: str) -> None:
        _LOGGER.warning(message)
        self.errors.append(f"{dt_util.utcnow().isoformat()} {message}")

    # ------------------------------------------------------------------ #
    async def _async_update_data(self) -> PrintCostData:  # pragma: no cover - push only
        return self.data
