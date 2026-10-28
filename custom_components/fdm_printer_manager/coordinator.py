"""Glue between Home Assistant and the pure business logic.

* reads the printer: Klipper directly over Moonraker's HTTP API (polled),
  Bambu Lab through the ha-bambulab entities (push);
* listens to the plug, power, energy and price entities (push);
* also samples on a timer (default 30 s) so long flat periods are integrated;
* feeds :class:`PrintTracker`, persists, stores finished jobs, fires the event;
* keeps the ha-bambulab thumbnail of the last print (the cover image entity
  moves on to the next job, this copy does not).

Business rules live in ``tracker.py`` and ``models/``.
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

from homeassistant.components.image import async_get_image
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .adapters import bambu
from .adapters.electricity import ElectricityReader, ElectricitySources
from .adapters.moonraker import DEFAULT_PORT, KlipperSource, MoonrakerClient, default_web_url
from .const import (
    BAMBU_ENTITY_KEYS,
    CONF_API_KEY,
    CONF_COVER_IMAGE_ENTITY,
    CONF_CURRENCY,
    CONF_ENERGY_ENTITY,
    CONF_HOST,
    CONF_INTEGRATION_METHOD,
    CONF_JOB_NAME_ENTITY,
    CONF_PORT,
    CONF_POWER_ENTITY,
    CONF_PRICE_ENTITY,
    CONF_PRINT_STATUS_ENTITY,
    CONF_PRINTER_NAME,
    CONF_PRINTER_SERIAL,
    CONF_PRINTER_TYPE,
    CONF_PROGRESS_ENTITY,
    CONF_REMAINING_TIME_ENTITY,
    CONF_SWITCH_ENTITY,
    CONF_UPDATE_INTERVAL,
    CONF_WEB_URL,
    DEFAULT_INTEGRATION_METHOD,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    EVENT_PRINT_FINISHED,
    HTTP_TIMEOUT,
    KLIPPER_POLL_INTERVAL,
    MAX_SAMPLE_GAP,
    IntegrationMethod,
    PrinterType,
)
from .models import JobPhase, PrintJob
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
        # Entries created before 0.3 have no type: they are Bambu.
        self.printer_type = PrinterType(self.conf.get(CONF_PRINTER_TYPE) or PrinterType.BAMBU)
        self.currency: str = self.conf.get(CONF_CURRENCY) or hass.config.currency

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
        self.klipper: KlipperSource | None = None
        if self.printer_type is PrinterType.KLIPPER:
            self.klipper = KlipperSource(
                MoonrakerClient(
                    session,
                    self.conf[CONF_HOST],
                    int(self.conf.get(CONF_PORT) or DEFAULT_PORT),
                    self.conf.get(CONF_API_KEY),
                    timeout=HTTP_TIMEOUT,
                )
            )
        self._polling = False

        self.last_image: bytes | None = None

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
        self.last_image = await self.store.async_load_image() if self.state.last_job else None
        self.tracker.restore(self.state.active_job)

        keys = (CONF_SWITCH_ENTITY, CONF_POWER_ENTITY, CONF_ENERGY_ENTITY, CONF_PRICE_ENTITY)
        if self.printer_type is PrinterType.BAMBU:
            keys = (*BAMBU_ENTITY_KEYS, *keys)
        watched = [self.conf[k] for k in keys if self.conf.get(k)]
        if watched:
            self._unsubs.append(async_track_state_change_event(self.hass, watched, self._on_state_change))
        if self.klipper is not None:
            await self.klipper.async_update()
            self._unsubs.append(
                async_track_time_interval(self.hass, self._async_poll_klipper, timedelta(seconds=KLIPPER_POLL_INTERVAL))
            )
        interval = timedelta(seconds=int(self.conf.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)))
        self._unsubs.append(async_track_time_interval(self.hass, self._on_tick, interval))

        # Jobs that were being stored when HA stopped.
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

    async def _async_poll_klipper(self, now: datetime) -> None:
        """Read the Klipper printer, then run the tracker. Skips a tick if the last poll is still running."""
        if self.klipper is None or self._polling:
            return
        self._polling = True
        try:
            await self.klipper.async_update()
        finally:
            self._polling = False
        self._process(dt_util.utcnow())

    # ------------------------------------------------------------------ #
    # Printer identity (used by the entities' device info)
    # ------------------------------------------------------------------ #
    @property
    def manufacturer(self) -> str:
        """Shown on the device page."""
        return "Klipper" if self.printer_type is PrinterType.KLIPPER else "Bambu Lab"

    @property
    def configuration_url(self) -> str | None:
        """Web interface of the printer ("Visit" on the device page). Bambu has none."""
        if self.printer_type is PrinterType.KLIPPER:
            return self.conf.get(CONF_WEB_URL) or default_web_url(self.conf[CONF_HOST])
        return None

    def build_snapshot(self, now: datetime) -> PrinterSnapshot:
        """Read the printer and the electrical inputs."""
        if self.klipper is not None:
            reading = self.klipper.reading
            return PrinterSnapshot(
                timestamp=now,
                status=reading.state,
                electricity=self.reader.sample(now),
                job_name=reading.filename,
                progress=reading.progress,
                remaining_min=self.klipper.remaining_min,
            )
        hass = self.hass
        return PrinterSnapshot(
            timestamp=now,
            status=bambu.read_raw_status(hass, self.conf.get(CONF_PRINT_STATUS_ENTITY)),
            electricity=self.reader.sample(now),
            job_name=bambu.read_text(hass, self.conf.get(CONF_JOB_NAME_ENTITY)),
            progress=bambu.read_number(hass, self.conf.get(CONF_PROGRESS_ENTITY)),
            remaining_min=bambu.read_remaining_minutes(hass, self.conf.get(CONF_REMAINING_TIME_ENTITY)),
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
            elif transition.kind is TransitionKind.ENDED:
                save_now = True
                self.state.finalizing.append(job)
                self._spawn(self._async_finalize(job, capture_image=True))

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
    # End of a print
    # ------------------------------------------------------------------ #
    async def _async_finalize(self, job: PrintJob, *, capture_image: bool = False) -> None:
        """Store the finished job as the last print and announce it.

        The thumbnail is read right away: after a restart the cover image may
        already show another job, so a resumed finalisation keeps none.
        """
        image = await self._async_read_cover_image() if capture_image else None
        job.phase = JobPhase.COMPLETED
        if job in self.state.finalizing:
            self.state.finalizing.remove(job)
        self.state.last_job = job
        self.last_image = image
        summary = job.summary()
        await self.store.async_save(self.state)
        await self.store.async_save_image(image)

        payload = {"printer": self.printer_name, "currency": self.currency, **summary}
        self.hass.bus.async_fire(EVENT_PRINT_FINISHED, {"entry_id": self.config_entry.entry_id, **payload})
        for listener in list(self._finished_listeners):
            listener(payload)
        _LOGGER.info(
            "Print '%s' finished: result=%s energy=%s kWh cost=%s %s",
            job.name,
            job.result,
            summary["energy_kwh"],
            summary["electricity_cost"],
            self.currency,
        )
        self._publish(dt_util.utcnow(), self.reader.price_per_kwh())

    async def _async_read_cover_image(self) -> bytes | None:
        """Bytes of the ha-bambulab cover image, ``None`` if there is none."""
        entity_id = self.conf.get(CONF_COVER_IMAGE_ENTITY)
        if not entity_id:
            return None
        try:
            image = await async_get_image(self.hass, entity_id)
        except Exception as err:  # noqa: BLE001 - a missing thumbnail is not an error
            _LOGGER.debug("No cover image from %s: %s", entity_id, err)
            return None
        return image.content

    def _record_error(self, message: str) -> None:
        _LOGGER.warning(message)
        self.errors.append(f"{dt_util.utcnow().isoformat()} {message}")

    # ------------------------------------------------------------------ #
    async def _async_update_data(self) -> PrintCostData:  # pragma: no cover - push only
        return self.data
