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

from homeassistant.components import persistent_notification
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
from .adapters.moonraker import (
    DEFAULT_PORT,
    KlipperSource,
    MoonrakerClient,
    MoonrakerError,
    Webcam,
    default_web_url,
)
from .alerts import AlertSender
from .const import (
    ACTIVE_STATES,
    BAMBU_ENTITY_KEYS,
    CONF_ACCESS_CODE,
    CONF_ALERTS,
    CONF_API_KEY,
    CONF_CLOSE_MINUTES,
    CONF_COVER_IMAGE_ENTITY,
    CONF_CURRENCY,
    CONF_DOWN_MINUTES,
    CONF_ENERGY_ENTITY,
    CONF_HOST,
    CONF_INTEGRATION_METHOD,
    CONF_JOB_NAME_ENTITY,
    CONF_NOTIFY,
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
    CONF_STALL_MINUTES,
    CONF_SWITCH_ENTITY,
    CONF_UPDATE_INTERVAL,
    CONF_WEB_URL,
    DEFAULT_CLOSE_MINUTES,
    DEFAULT_DOWN_MINUTES,
    DEFAULT_INTEGRATION_METHOD,
    DEFAULT_STALL_MINUTES,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    EVENT_PRINT_FINISHED,
    EVENT_PRINT_MARKED,
    HISTORY_LIMIT,
    HTTP_TIMEOUT,
    KLIPPER_POLL_INTERVAL,
    MAX_SAMPLE_GAP,
    PAUSED_STATES,
    SAFE_POWER_COOL_TEMP,
    SAFE_POWER_COOL_TIMEOUT,
    SAFE_POWER_POLL,
    SAFE_POWER_WAIT,
    WEBCAM_CHECK_INTERVAL,
    IntegrationMethod,
    PrinterType,
)
from .models import ElectricityAccumulator, JobPhase, PrintJob, PrintResult
from .outcome import MARKABLE, mark_history
from .storage import JobStore, StoredState
from .tracker import (
    PrinterActivity,
    PrinterSnapshot,
    PrintTracker,
    Transition,
    TransitionKind,
    classify_status,
)
from .watchdog import AlertKind, WatchAction, Watchdog, WatchInput

_LOGGER = logging.getLogger(__name__)

# ha-bambulab button keys for each action
_BAMBU_BUTTONS: dict[str, tuple[str, ...]] = {
    "pause": ("pause",),
    "resume": ("resume",),
    "cancel": ("stop", "cancel"),
}
# ha-bambulab sensor keys for the nozzle temperature
_BAMBU_NOZZLE_KEYS: tuple[str, ...] = ("nozzle_temp", "nozzle_temperature")
# ha-bambulab camera: a camera entity (X1 / H2), or an image entity refreshed from the camera (A1 / P1)
_BAMBU_CAMERA_KEYS: tuple[str, ...] = ("camera", "p1p_camera", "chamber_image")

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
                on_auth_failed=self._on_bambu_auth_failed,
            )
        self._last_bambu_process: float = 0.0
        self._last_bambu_state: str | None = None
        self._polling = False
        self._last_snapshot: PrinterSnapshot | None = None
        # Safe power: "on" / "off" follow the plug; "cooling" while the nozzle cools down,
        # then "shutting_down" while waiting to cut it.
        self.power_phase: str | None = None
        self.power_cut_at: datetime | None = None
        self._power_task: asyncio.Task | None = None
        # Klipper camera found through Moonraker (None: no camera, or not checked yet).
        self.webcam: Webcam | None = None
        self._webcam_checked = False

        self.last_image: bytes | None = None

        # Watching a running print (printer down, stuck or interrupted print, no progress).
        self.alerts_on: bool = bool(self.conf.get(CONF_ALERTS, True))
        self.close_minutes = int(self.conf.get(CONF_CLOSE_MINUTES) or DEFAULT_CLOSE_MINUTES)
        stall = self.conf.get(CONF_STALL_MINUTES)
        self.watchdog = Watchdog(
            down_after_s=60 * int(self.conf.get(CONF_DOWN_MINUTES) or DEFAULT_DOWN_MINUTES),
            close_after_s=60 * self.close_minutes,
            # 0 turns the "no progress" warning off
            stall_after_s=60 * int(DEFAULT_STALL_MINUTES if stall is None else stall) or float("inf"),
        )
        self.alerts = AlertSender(hass, entry.entry_id, self.printer_name, self.conf.get(CONF_NOTIFY))

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
            await self.async_check_webcam()
            self._unsubs.append(
                async_track_time_interval(self.hass, self._async_poll_klipper, timedelta(seconds=KLIPPER_POLL_INTERVAL))
            )
            self._unsubs.append(
                async_track_time_interval(self.hass, self._async_webcam_tick, timedelta(seconds=WEBCAM_CHECK_INTERVAL))
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

    def _bambu_entity(self, domain: str, keys: tuple[str, ...]) -> str | None:
        """An entity of the ha-bambulab device, found by translation key."""
        device_id = self.conf.get(CONF_PRINTER_DEVICE_ID)
        if not device_id:
            return None
        for entry in er.async_entries_for_device(er.async_get(self.hass), device_id):
            if entry.domain == domain and (entry.translation_key in keys or entry.unique_id.endswith(keys)):
                return entry.entity_id
        return None

    def _bambu_button(self, keys: tuple[str, ...]) -> str | None:
        """A button of the ha-bambulab device (pause / resume / stop)."""
        return self._bambu_entity("button", keys)

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
        """Safe power off: never while printing, never with a hot nozzle; Klipper shuts its computer down first."""
        if not self.switch_entity:
            raise HomeAssistantError(f"{self.printer_name} has no smart plug")
        if self.is_printing:
            raise HomeAssistantError(f"{self.printer_name} is printing: it will not be turned off")
        if self.power_phase is not None:  # already cooling or shutting down
            return
        if self.nozzle_is_hot:
            self.power_phase = "cooling"
            self._publish(dt_util.utcnow(), self.reader.price_per_kwh())
            self._power_task = self.config_entry.async_create_background_task(
                self.hass, self._async_cool_then_power_off(), f"{DOMAIN}_safe_power"
            )
            return
        await self._async_power_off_cool()

    @property
    def nozzle_is_hot(self) -> bool:
        """Nozzle at or above SAFE_POWER_COOL_TEMP. No reading (printer off or offline) counts as cool."""
        temp = self._nozzle_temp()
        return temp is not None and temp >= SAFE_POWER_COOL_TEMP

    async def _async_cool_then_power_off(self) -> None:
        """Wait for the nozzle to cool, then power off as usual; give up if it stays hot."""
        deadline = dt_util.utcnow() + timedelta(seconds=SAFE_POWER_COOL_TIMEOUT)
        cooled = False
        try:
            while dt_util.utcnow() < deadline:
                await asyncio.sleep(SAFE_POWER_POLL)
                if self.is_printing:  # someone started a print meanwhile
                    _LOGGER.info("%s: print started, plug left on", self.printer_name)
                    return
                if self.klipper is not None:
                    await self.klipper.async_update()
                if not self.nozzle_is_hot:
                    cooled = True
                    break
                self._publish(dt_util.utcnow(), self.reader.price_per_kwh())
            if not cooled:
                message = (
                    f"{self.printer_name}: nozzle still at {self._nozzle_temp():.0f} °C after "
                    f"{SAFE_POWER_COOL_TIMEOUT // 60} min, so a heater may be on. The plug was left on."
                )
                _LOGGER.warning(message)
                persistent_notification.async_create(
                    self.hass,
                    message,
                    title="FDM Printer Manager",
                    notification_id=f"{DOMAIN}_hot_{self.config_entry.entry_id}",
                )
                return
        finally:
            self.power_phase = None
            self._power_task = None
            if not cooled:
                self._publish(dt_util.utcnow(), self.reader.price_per_kwh())
        await self._async_power_off_cool()

    async def _async_power_off_cool(self) -> None:
        """The nozzle is cool: Klipper shuts its computer down first, other printers are cut now."""
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
    # Marking a finished print by hand
    # ------------------------------------------------------------------ #
    async def async_mark_result(self, result: str, job_id: str | None = None) -> None:
        """Mark a finished print (the last one when no job id is given) as a success or a failure."""
        if result not in MARKABLE:
            raise HomeAssistantError(f"A print can only be marked as {' or '.join(MARKABLE)}")
        last = self.state.last_job
        job_id = job_id or (last.job_id if last else None)
        if job_id is None:
            raise HomeAssistantError(f"{self.printer_name} has no finished print yet")
        found = mark_history(self.state.history, job_id, result)
        if last is not None and last.job_id == job_id:
            last.result = PrintResult(result)
            last.result_by = "user"
            found = True
        if not found:
            raise HomeAssistantError(f"{self.printer_name}: print {job_id} not found")
        await self.store.async_save(self.state)
        self.hass.bus.async_fire(
            EVENT_PRINT_MARKED,
            {"entry_id": self.config_entry.entry_id, "printer": self.printer_name, "job_id": job_id, "result": result},
        )
        _LOGGER.info("%s: print %s marked as %s", self.printer_name, job_id, result)
        self._publish(dt_util.utcnow(), self.reader.price_per_kwh())

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
        if not self.klipper.available:
            self._webcam_checked = False  # look again when it comes back
        elif not self._webcam_checked:
            self._spawn(self.async_check_webcam())
        self._process(dt_util.utcnow())

    async def _async_webcam_tick(self, now: datetime) -> None:
        await self.async_check_webcam()

    async def async_check_webcam(self) -> None:
        """Look for an enabled camera on the Klipper printer: at start, when it comes back, every 30 minutes."""
        if self.klipper is None or not self.klipper.available:
            return
        self._webcam_checked = True
        try:
            cams = await self.klipper.client.webcams(self.configuration_url or default_web_url(self.conf[CONF_HOST]))
        except MoonrakerError as err:
            _LOGGER.debug("%s: cannot list webcams: %s", self.printer_name, err)
            self._webcam_checked = False
            return
        except Exception:  # a camera check must never break the printer
            _LOGGER.exception("%s: webcam check failed", self.printer_name)
            return
        webcam = cams[0] if cams else None
        if webcam == self.webcam:
            return
        _LOGGER.info("%s: camera %s", self.printer_name, f"found ({webcam.name})" if webcam else "gone")
        self.webcam = webcam
        self._publish(dt_util.utcnow(), self.reader.price_per_kwh())

    @property
    def camera_entity(self) -> str | None:
        """Camera the panel shows: ours for Klipper, ha-bambulab's own for Bambu (never a second one)."""
        if self.klipper is not None:
            if self.webcam is None:
                return None
            return er.async_get(self.hass).async_get_entity_id("camera", DOMAIN, f"{self.config_entry.entry_id}_camera")
        if self.printer_type is PrinterType.BAMBU:
            return self._bambu_entity("camera", _BAMBU_CAMERA_KEYS) or self._bambu_entity("image", _BAMBU_CAMERA_KEYS)
        return None

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

    @callback
    def _on_bambu_auth_failed(self) -> None:
        """The access code was refused (it changes when LAN Only is turned off and on): ask for the new one."""
        _LOGGER.warning("%s: access code refused, asking for the new one", self.printer_name)
        self.config_entry.async_start_reauth(self.hass)

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
        if self.alerts_on:
            transitions.extend(self._watch(snap, transitions))
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

    @property
    def reachable(self) -> bool:
        """The printer answers (Klipper / Bambu LAN connection, or the ha-bambulab status is not offline)."""
        source = self.klipper or self.bambu_lan
        if source is not None:
            return source.available
        status = (self.printer_status or "").strip().lower()
        return status not in ("", "unavailable", "unknown", "offline")

    def _watch(self, snap: PrinterSnapshot, transitions: list[Transition]) -> list[Transition]:
        """Run the watchdog after the tracker; returns the print it closes, if any."""
        for transition in transitions:
            # A Bambu printer comes back "idle" (or "failed") after a power cut: the tracker has just ended
            # the print, without a verdict. It was interrupted.
            ended = transition.job
            if (
                transition.kind is TransitionKind.ENDED
                and self.watchdog.ended_after_power_loss()
                and ended.result in (PrintResult.UNKNOWN, PrintResult.FAILED)
            ):
                ended.result = PrintResult.FAILED
                ended.result_by = "watchdog"
                ended.remarks.append("Interrupted: the printer lost power or the connection and did not resume")
                self.alerts.send(
                    WatchAction(AlertKind.INTERRUPTED, close_job=True, after_power_loss=True, status=snap.status),
                    ended.name,
                    ended.progress,
                    self.close_minutes,
                )
        closed: list[Transition] = []
        job = self.tracker.active_job
        for action in self.watchdog.check(
            WatchInput(
                now=snap.timestamp,
                job_open=job is not None,
                reachable=self.reachable,
                printing=classify_status(snap.status) is PrinterActivity.ACTIVE,
                progress=snap.progress,
                status=snap.status,
            )
        ):
            if action.close_job and job is not None and (transition := self.tracker.close(snap, PrintResult.FAILED)):
                job.result_by = "watchdog"
                job.remarks.append(
                    "Interrupted: the printer lost power or the connection and did not resume"
                    if action.after_power_loss
                    else f"Closed by the integration: the printer was not printing for {action.minutes} min"
                )
                closed.append(transition)
            self.alerts.send(action, job.name if job else None, snap.progress, self.close_minutes)
        return closed

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
        return bambu.read_number(self.hass, self._bambu_entity("sensor", _BAMBU_NOZZLE_KEYS))

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
