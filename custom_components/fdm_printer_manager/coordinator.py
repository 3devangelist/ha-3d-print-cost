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
from homeassistant.const import STATE_ON
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_time_interval,
)
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
from .adapters.reach import host_is_up
from .adapters.ssh import (
    COSMOS_LOGIN,
    DEFAULT_SSH_COMMAND,
    SshError,
    SshLogin,
    needs_cosmos_login,
    ssh_shutdown,
)
from .const import (
    ACTIVE_STATES,
    BAMBU_ENTITY_KEYS,
    CONF_ACCESS_CODE,
    CONF_API_KEY,
    CONF_AUTO_OFF,
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
    CONF_SSH_COMMAND,
    CONF_SSH_KEY,
    CONF_SSH_PASSWORD,
    CONF_SSH_USERNAME,
    CONF_SWITCH_ENTITY,
    CONF_UPDATE_INTERVAL,
    CONF_WEB_URL,
    DEFAULT_AUTO_OFF,
    DEFAULT_INTEGRATION_METHOD,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    EVENT_PRINT_FINISHED,
    HISTORY_LIMIT,
    HTTP_TIMEOUT,
    KLIPPER_POLL_INTERVAL,
    MAX_SAMPLE_GAP,
    PAUSED_STATES,
    SAFE_POWER_COOL_TEMP,
    SAFE_POWER_COOL_TIMEOUT,
    SAFE_POWER_DOWN_CHECKS,
    SAFE_POWER_GRACE,
    SAFE_POWER_POLL,
    SAFE_POWER_WAIT,
    WEBCAM_CHECK_INTERVAL,
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
            )
        self._last_bambu_process: float = 0.0
        self._last_bambu_state: str | None = None
        self._polling = False
        self._last_snapshot: PrinterSnapshot | None = None
        # Safe power: "on" / "off" follow the plug; "waiting" after a print (auto-off),
        # "cooling" while the nozzle cools down, then "shutting_down" until the computer is off.
        self.power_phase: str | None = None
        self.power_cut_at: datetime | None = None
        self.auto_off_at: datetime | None = None
        # How the last power off ended: power_off, aborted, hot (nozzle stayed hot), still_on (computer kept running)
        self.power_result: str | None = None
        self._power_task: asyncio.Task | None = None
        self._auto_off_unsub: CALLBACK_TYPE | None = None
        # Klipper camera found through Moonraker (None: no camera, or not checked yet).
        self.webcam: Webcam | None = None
        self._webcam_checked = False

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
        """Turn the plug on (cancels a pending auto-off or safe shutdown)."""
        if not self.switch_entity:
            raise HomeAssistantError(f"{self.printer_name} has no smart plug")
        if self.power_phase == "waiting":
            self._cancel_auto_off("turned on by hand")
        if self._power_task is not None:
            self._power_task.cancel()
        await self.hass.services.async_call(
            "homeassistant", "turn_on", {"entity_id": self.switch_entity}, blocking=True
        )

    @property
    def auto_off_minutes(self) -> int:
        """Minutes after a print ends before the printer is turned off (0 = never)."""
        return int(self.conf.get(CONF_AUTO_OFF, DEFAULT_AUTO_OFF) or 0)

    @property
    def plug_is_on(self) -> bool:
        """The smart plug is on."""
        state = self.hass.states.get(self.switch_entity) if self.switch_entity else None
        return state is not None and state.state == STATE_ON

    @callback
    def _start_auto_off(self) -> None:
        """A print ended: turn the printer off safely after ``auto_off_minutes`` (a timer, no task while waiting)."""
        if self.auto_off_minutes <= 0 or self.power_phase is not None or not self.plug_is_on:
            return
        self.power_phase = "waiting"
        self.auto_off_at = dt_util.utcnow() + timedelta(minutes=self.auto_off_minutes)
        self.power_result = None
        self._auto_off_unsub = async_track_point_in_utc_time(self.hass, self._on_auto_off_due, self.auto_off_at)

    @callback
    def _check_auto_off(self) -> None:
        """While waiting: a new print, or the plug turned off by hand, cancels the auto-off."""
        if self.power_phase != "waiting":
            return
        if self.is_printing:
            self._cancel_auto_off("a new print started")
        elif not self.plug_is_on:
            self._cancel_auto_off("the plug was turned off")

    @callback
    def _cancel_auto_off(self, why: str) -> None:
        if self._auto_off_unsub is not None:
            self._auto_off_unsub()
            self._auto_off_unsub = None
        self._end_power_off("aborted", why)

    @callback
    def _on_auto_off_due(self, now: datetime) -> None:
        self._auto_off_unsub = None
        self.power_phase = None
        self.auto_off_at = None
        self._power_task = self.config_entry.async_create_background_task(
            self.hass, self._async_auto_off(), f"{DOMAIN}_auto_off"
        )

    async def _async_auto_off(self) -> None:
        """The usual safe power off (cool, shut the computer down, cut once it is off)."""
        self._power_task = None
        try:
            await self.async_power_off()
        except HomeAssistantError as err:
            self._end_power_off("aborted", str(err))

    @callback
    def _end_power_off(self, result: str, why: str) -> None:
        _LOGGER.info("%s: power off %s (%s)", self.printer_name, result, why)
        self.power_result = result
        self.power_phase = None
        self.power_cut_at = None
        self.auto_off_at = None
        self._power_task = None
        self._publish(dt_util.utcnow(), self.reader.price_per_kwh())

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
                self.power_result = "hot"
                return
        finally:
            self.power_phase = None
            self._power_task = None
            if not cooled:
                self._publish(dt_util.utcnow(), self.reader.price_per_kwh())
        await self._async_power_off_cool()

    @property
    def ssh_login(self) -> SshLogin | None:
        """The SSH login from the options, ``None`` when there is none."""
        username = (self.conf.get(CONF_SSH_USERNAME) or "").strip()
        if not username:
            return None
        return SshLogin(
            username=username,
            password=self.conf.get(CONF_SSH_PASSWORD) or None,
            private_key=self.conf.get(CONF_SSH_KEY) or None,
            command=(self.conf.get(CONF_SSH_COMMAND) or "").strip() or DEFAULT_SSH_COMMAND,
        )

    async def _async_shutdown_login(self, moonraker_error: str | None) -> SshLogin | None:
        """SSH login for the shutdown: the one from the options, else the Elegoo "cosmos" one when recognised."""
        if (login := self.ssh_login) is not None:
            return login
        hostname: str | None = None
        if self.klipper is not None and self.klipper.available:
            try:
                hostname = await self.klipper.client.hostname()
            except Exception:  # noqa: BLE001 - the error text may still tell
                hostname = None
        if needs_cosmos_login(hostname, moonraker_error):
            _LOGGER.info("%s: Elegoo cosmos firmware, shutting down over SSH as root", self.printer_name)
            return COSMOS_LOGIN
        return None

    async def _async_power_off_cool(self) -> None:
        """The nozzle is cool: Klipper shuts its computer down first, other printers are cut now.

        Moonraker's ``machine.shutdown`` first; if it fails, the SSH login from
        the options. The plug is then cut only once the computer stops answering.
        """
        if self.klipper is None:
            await self._async_cut_plug()
            return
        host = self.conf[CONF_HOST]
        sent = False
        moonraker_error: str | None = None
        if self.klipper.available:
            try:
                await self.klipper.client.shutdown_host()
                sent = True
            except Exception as err:  # noqa: BLE001 - SSH, or the wait below, decides
                moonraker_error = str(err)
                _LOGGER.warning("%s: Moonraker shutdown request failed: %s", self.printer_name, err)
        elif not await host_is_up(host, int(self.conf.get(CONF_PORT) or DEFAULT_PORT)):
            await self._async_cut_plug()  # the computer is already off
            return
        if not sent and (login := await self._async_shutdown_login(moonraker_error)) is not None:
            try:
                await ssh_shutdown(host, login)
                sent = True
            except SshError as err:
                _LOGGER.warning("%s: SSH shutdown failed: %s", self.printer_name, err)
        if not sent and not self.klipper.available:
            await self._async_cut_plug()  # Moonraker gone and no SSH: as before 0.8, cut now
            return
        if not sent:
            # Moonraker answers but cannot shut down (the Elegoo Centauri Carbon replies
            # HTTP 500 "Error running shell command") and SSH failed or is not set up:
            # cutting now would kill a running computer, so the plug stays on.
            message = (
                f"{self.printer_name}: the printer's computer could not be shut down (Moonraker refused and "
                "SSH failed or is not set up), so the plug was left on. "
                "Add or check the SSH login in the printer's options."
            )
            _LOGGER.warning(message)
            persistent_notification.async_create(
                self.hass,
                message,
                title="FDM Printer Manager",
                notification_id=f"{DOMAIN}_no_shutdown_{self.config_entry.entry_id}",
            )
            self.power_result = "no_shutdown"
            self._publish(dt_util.utcnow(), self.reader.price_per_kwh())
            return
        self.power_phase = "shutting_down"
        self.power_cut_at = dt_util.utcnow() + timedelta(seconds=SAFE_POWER_WAIT)
        self._publish(dt_util.utcnow(), self.reader.price_per_kwh())
        self._power_task = self.config_entry.async_create_background_task(
            self.hass, self._async_wait_then_cut(), f"{DOMAIN}_safe_power"
        )

    async def _async_wait_then_cut(self) -> None:
        """Cut the plug once the printer's computer stops answering; never while it still runs.

        Moonraker stops early in the shutdown, so the computer itself is checked
        (ping, or a TCP connection to it). Still answering after SAFE_POWER_WAIT:
        the plug is left on and a notification says so.
        """
        host = self.conf[CONF_HOST]
        port = int(self.conf.get(CONF_PORT) or DEFAULT_PORT)
        deadline = dt_util.utcnow() + timedelta(seconds=SAFE_POWER_WAIT)
        try:
            silent = 0
            while silent < SAFE_POWER_DOWN_CHECKS:
                if dt_util.utcnow() >= deadline:
                    message = (
                        f"{self.printer_name}: still answering {SAFE_POWER_WAIT // 60} min after the shutdown "
                        "request, so it was not switched off. The plug was left on."
                    )
                    _LOGGER.warning(message)
                    persistent_notification.async_create(
                        self.hass,
                        message,
                        title="FDM Printer Manager",
                        notification_id=f"{DOMAIN}_still_on_{self.config_entry.entry_id}",
                    )
                    self.power_result = "still_on"
                    return
                await asyncio.sleep(SAFE_POWER_POLL)
                if self.is_printing:  # someone started a print meanwhile
                    _LOGGER.info("%s: print started, plug left on", self.printer_name)
                    self.power_result = "aborted"
                    return
                silent = 0 if await host_is_up(host, port) else silent + 1
            await asyncio.sleep(SAFE_POWER_GRACE)  # let the last writes reach the disk
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
        self.power_result = "power_off"
        _LOGGER.info("%s: plug turned off", self.printer_name)

    async def async_stop(self) -> None:
        """Unsubscribe, cancel background work, flush to disk."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._auto_off_unsub is not None:
            self._auto_off_unsub()
            self._auto_off_unsub = None
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
                self._start_auto_off()
        self._check_auto_off()

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
