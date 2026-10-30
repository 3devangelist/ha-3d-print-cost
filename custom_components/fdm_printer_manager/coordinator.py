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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .adapters import bambu
from .adapters.bambu_lan import BambuLanClient
from .adapters.electricity import ElectricityReader, ElectricitySources
from .adapters.moonraker import DEFAULT_PORT, KlipperSource, MoonrakerClient, default_web_url
from .const import (
    ACTIVE_STATES,
    BAMBU_ENTITY_KEYS,
    CONF_ACCESS_CODE,
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
    CONF_PRICE_FIXED,
    CONF_PRINT_STATUS_ENTITY,
    CONF_PRINTER_DEVICE_ID,
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
    HISTORY_LIMIT,
    HTTP_TIMEOUT,
    KLIPPER_POLL_INTERVAL,
    MAX_SAMPLE_GAP,
    PAUSED_STATES,
    SAFE_POWER_POLL,
    SAFE_POWER_WAIT,
    IntegrationMethod,
    PrinterType,
)
from .models import ElectricityAccumulator, JobPhase, PrintJob
from .storage import JobStore, StoredState
from .tracker import PrinterSnapshot, PrintTracker, TransitionKind

_LOGGER = logging.getLogger(__name__)

# ha-bambulab button keys for each action
_BAMBU_BUTTONS: dict[str, tuple[str, ...]] = {
    "pause": ("pause",),
    "resume": ("resume",),
    "cancel": ("stop", "cancel"),
}

type PrintCostConfigEntry = ConfigEntry[PrintCostCoordinator]


@dataclass(slots=True)
class PrintCostData:
    """What entities read after each update."""

    now: datetime
    active_job: PrintJob | None
    finalizing_job: PrintJob | None
    last_job: PrintJob | None
    price_per_kwh: Decimal | None
    # Live printer reading (shown by the panel even when nothing is printing)
    printer_status: str | None = None
    progress: float | None = None
    filename: str | None = None
    remaining_min: float | None = None
    nozzle_temp: float | None = None
    # The printer's own counters (since it was added / since midnight)
    energy_total_kwh: float | None = None
    cost_total: Decimal | None = None
    energy_today_kwh: float | None = None
    cost_today: Decimal | None = None

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
                price_fixed=self.conf.get(CONF_PRICE_FIXED),
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
        self.bambu_lan: BambuLanClient | None = None
        if self.printer_type is PrinterType.BAMBU_LAN:
            self.bambu_lan = BambuLanClient(
                self.conf[CONF_HOST],
                self.conf[CONF_PRINTER_SERIAL],
                self.conf[CONF_ACCESS_CODE],
                self._on_bambu_update,
            )
        self._last_bambu_process: float = 0.0
        self._last_bambu_state: str | None = None
        self._polling = False
        self._last_snapshot: PrinterSnapshot | None = None
        # Safe power: "on" / "off" follow the plug; "shutting_down" while waiting to cut it.
        self.power_phase: str | None = None
        self.power_cut_at: datetime | None = None
        self._power_task: asyncio.Task | None = None

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
        if self.bambu_lan is not None:
            self.bambu_lan.start(
                lambda coro: self.config_entry.async_create_background_task(self.hass, coro, f"{DOMAIN}_bambu_lan")
            )
        interval = timedelta(seconds=int(self.conf.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)))
        self._unsubs.append(async_track_time_interval(self.hass, self._on_tick, interval))

        # Jobs that were being stored when HA stopped.
        for job in list(self.state.finalizing):
            self._spawn(self._async_finalize(job))

        self._process(dt_util.utcnow())

    # ------------------------------------------------------------------ #
    # Printer state and controls
    # ------------------------------------------------------------------ #
    @property
    def printer_status(self) -> str | None:
        """Raw state reported by the printer at the last reading."""
        return self._last_snapshot.status if self._last_snapshot else None

    @property
    def is_printing(self) -> bool:
        """Printing or paused: the plug must not be cut and Cancel makes sense."""
        return (self.printer_status or "").lower() in ACTIVE_STATES

    @property
    def is_paused(self) -> bool:
        """Paused print (Resume makes sense)."""
        return (self.printer_status or "").lower() in PAUSED_STATES

    @property
    def switch_entity(self) -> str | None:
        """The smart plug's switch, if any."""
        return self.conf.get(CONF_SWITCH_ENTITY)

    def _bambu_button(self, keys: tuple[str, ...]) -> str | None:
        """A button of the ha-bambulab device (pause / resume / stop), found by translation key."""
        device_id = self.conf.get(CONF_PRINTER_DEVICE_ID)
        if not device_id:
            return None
        for entry in er.async_entries_for_device(er.async_get(self.hass), device_id):
            if entry.domain == "button" and (entry.translation_key in keys or entry.unique_id.endswith(keys)):
                return entry.entity_id
        return None

    def can_control(self, action: str) -> bool:
        """Whether pause / resume / cancel is available for this printer right now."""
        if action == "pause" and (not self.is_printing or self.is_paused):
            return False
        if action == "resume" and not self.is_paused:
            return False
        if action == "cancel" and not self.is_printing:
            return False
        if self.klipper is not None:
            return self.klipper.available
        if self.bambu_lan is not None:
            return self.bambu_lan.available
        return self._bambu_button(_BAMBU_BUTTONS[action]) is not None

    async def async_control(self, action: str) -> None:
        """Pause, resume or cancel the print on the printer itself."""
        if not self.can_control(action):
            raise HomeAssistantError(f"{self.printer_name}: cannot {action} now")
        if self.klipper is not None:
            await getattr(self.klipper.client, action)()
            await self.klipper.async_update()
            self._process(dt_util.utcnow())
            return
        if self.bambu_lan is not None:
            await self.bambu_lan.async_command({"pause": "pause", "resume": "resume", "cancel": "stop"}[action])
            return
        button = self._bambu_button(_BAMBU_BUTTONS[action])
        await self.hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)

    async def async_power_on(self) -> None:
        """Turn the plug on (cancels a pending safe shutdown)."""
        if not self.switch_entity:
            raise HomeAssistantError(f"{self.printer_name} has no smart plug")
        if self._power_task is not None:
            self._power_task.cancel()
        await self.hass.services.async_call(
            "homeassistant", "turn_on", {"entity_id": self.switch_entity}, blocking=True
        )

    async def async_power_off(self) -> None:
        """Safe power off: never while printing; Klipper shuts its computer down first."""
        if not self.switch_entity:
            raise HomeAssistantError(f"{self.printer_name} has no smart plug")
        if self.is_printing:
            raise HomeAssistantError(f"{self.printer_name} is printing: it will not be turned off")
        if self.power_phase == "shutting_down":
            return
        if self.klipper is None or not self.klipper.available:
            await self._async_cut_plug()
            return
        try:
            await self.klipper.client.shutdown_host()
        except Exception as err:  # noqa: BLE001 - the plug is still cut after the wait
            _LOGGER.warning("%s: host shutdown request failed: %s", self.printer_name, err)
        self.power_phase = "shutting_down"
        self.power_cut_at = dt_util.utcnow() + timedelta(seconds=SAFE_POWER_WAIT)
        self._publish(dt_util.utcnow(), self.reader.price_per_kwh())
        self._power_task = self.config_entry.async_create_background_task(
            self.hass, self._async_wait_then_cut(), f"{DOMAIN}_safe_power"
        )

    async def _async_wait_then_cut(self) -> None:
        """Cut the plug once the printer's computer is down (or after SAFE_POWER_WAIT)."""
        try:
            waited = 0
            down_since: int | None = None
            while waited < SAFE_POWER_WAIT:
                await asyncio.sleep(SAFE_POWER_POLL)
                waited += SAFE_POWER_POLL
                if self.is_printing:  # someone started a print meanwhile
                    _LOGGER.info("%s: print started, plug left on", self.printer_name)
                    return
                assert self.klipper is not None
                await self.klipper.async_update()
                if not self.klipper.available:
                    down_since = waited if down_since is None else down_since
                    if waited - down_since >= 15:  # give the OS a moment to finish
                        break
            await self._async_cut_plug()
        finally:
            self.power_phase = None
            self.power_cut_at = None
            self._power_task = None
            self._publish(dt_util.utcnow(), self.reader.price_per_kwh())

    async def _async_cut_plug(self) -> None:
        await self.hass.services.async_call(
            "homeassistant", "turn_off", {"entity_id": self.switch_entity}, blocking=True
        )
        _LOGGER.info("%s: plug turned off", self.printer_name)

    async def async_stop(self) -> None:
        """Unsubscribe, cancel background work, flush to disk."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for task in list(self._tasks):
            task.cancel()
        if self.bambu_lan is not None:
            await self.bambu_lan.stop()
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

    @callback
    def _on_bambu_update(self) -> None:
        """A Bambu LAN report arrived: run the tracker (at most every 2 s unless the state changed)."""
        if self.bambu_lan is None:
            return
        state = self.bambu_lan.reading.state
        now = self.hass.loop.time()
        if state == self._last_bambu_state and now - self._last_bambu_process < 2:
            return
        self._last_bambu_state = state
        self._last_bambu_process = now
        self._process(dt_util.utcnow())

    def build_snapshot(self, now: datetime) -> PrinterSnapshot:
        """Read the printer and the electrical inputs."""
        if self.bambu_lan is not None:
            reading = self.bambu_lan.reading
            return PrinterSnapshot(
                timestamp=now,
                status=reading.state,
                electricity=self.reader.sample(now),
                job_name=reading.filename,
                progress=reading.progress,
                remaining_min=reading.remaining_min,
            )
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
        self._last_snapshot = snap
        self._count(now, snap)
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
        elif self.state.active_job is not None or self.state.total is not None:
            self.store.async_delay_save(self.state)
        self._publish(now, snap.electricity.price_per_kwh)

    def _new_counter(self) -> ElectricityAccumulator:
        return ElectricityAccumulator(
            method=IntegrationMethod(self.conf.get(CONF_INTEGRATION_METHOD, DEFAULT_INTEGRATION_METHOD)),
            max_gap_s=MAX_SAMPLE_GAP,
        )

    def _count(self, now: datetime, snap: PrinterSnapshot) -> None:
        """Feed the printer's total and today counters (only when the plug measures energy)."""
        if not self.reader.sources.measures_energy:
            return
        state = self.state
        if state.total is None:
            state.total = self._new_counter()
        day = dt_util.as_local(now).date().isoformat()
        if state.today is None or state.today_date != day:
            if state.today is not None:
                state.today.add_sample(snap.electricity)  # close yesterday at midnight
            state.today = self._new_counter()
            state.today_date = day
        state.total.add_sample(snap.electricity)
        state.today.add_sample(snap.electricity)

    def _sync_state(self) -> None:
        self.state.active_job = self.tracker.active_job

    def _publish(self, now: datetime, price: Decimal | None) -> None:
        snap = self._last_snapshot
        self.async_set_updated_data(
            PrintCostData(
                now=now,
                active_job=self.tracker.active_job,
                finalizing_job=self.state.finalizing[-1] if self.state.finalizing else None,
                last_job=self.state.last_job,
                price_per_kwh=price,
                printer_status=snap.status if snap else None,
                progress=snap.progress if snap else None,
                filename=snap.job_name if snap else None,
                remaining_min=snap.remaining_min if snap else None,
                nozzle_temp=self._nozzle_temp(),
                energy_total_kwh=self.state.total.total_energy_kwh if self.state.total else None,
                cost_total=self._counter_cost(self.state.total),
                energy_today_kwh=self.state.today.total_energy_kwh if self.state.today else None,
                cost_today=self._counter_cost(self.state.today),
            )
        )

    def _nozzle_temp(self) -> float | None:
        if self.klipper is not None:
            return self.klipper.reading.nozzle_temp
        if self.bambu_lan is not None:
            return self.bambu_lan.reading.nozzle_temp
        return None

    @staticmethod
    def _counter_cost(acc: ElectricityAccumulator | None) -> Decimal | None:
        """Cost of a counter; unknown only when no kWh at all could be priced."""
        if acc is None:
            return None
        if acc.total_cost == 0 and acc.total_uncosted_energy_kwh > 0:
            return None
        return acc.total_cost

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
        self.state.history.insert(0, {"printer": self.printer_name, **summary})
        del self.state.history[HISTORY_LIMIT:]
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
