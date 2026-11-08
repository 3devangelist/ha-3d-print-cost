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
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from homeassistant.components import persistent_notification
from homeassistant.components.image import async_get_image
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .adapters import bambu
from .adapters.bambu_camera import BambuCamera, has_jpeg_camera
from .adapters.bambu_lan import BambuCommandRefused, BambuError, BambuLanClient, light_on
from .adapters.electricity import ElectricityReader, ElectricitySources
from .adapters.klipper_extras import (
    BABYSTEP_RESET,
    KlipperLight,
    babystep_gcode,
    light_color,
    light_gcode,
    light_level,
    z_offset,
)
from .adapters.moonraker import (
    DEFAULT_PORT,
    FileMetadata,
    KlipperSource,
    MoonrakerClient,
    MoonrakerError,
    Webcam,
    default_web_url,
    history_filament,
    url_host,
)
from .adapters.reach import host_is_up
from .adapters.ssh import (
    COSMOS_LOGIN,
    DEFAULT_SSH_COMMAND,
    SshError,
    SshHostKeyChanged,
    SshLogin,
    needs_cosmos_login,
    ssh_shutdown,
)
from .alerts import AlertSender
from .const import (
    ACTIVE_STATES,
    BAMBU_ENTITY_KEYS,
    CONF_ACCESS_CODE,
    CONF_ALERTS,
    CONF_API_KEY,
    CONF_AUTO_OFF,
    CONF_BABYSTEP,
    CONF_BABYSTEP_SAVE,
    CONF_CLOSE_MINUTES,
    CONF_COVER_IMAGE_ENTITY,
    CONF_CURRENCY,
    CONF_DOWN_MINUTES,
    CONF_ENERGY_ENTITY,
    CONF_FILAMENT_PRICE,
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
    CONF_SSH_COMMAND,
    CONF_SSH_KEY,
    CONF_SSH_PASSWORD,
    CONF_SSH_USERNAME,
    CONF_STALL_MINUTES,
    CONF_SWITCH_ENTITY,
    CONF_UPDATE_INTERVAL,
    CONF_WEB_URL,
    DEFAULT_AUTO_OFF,
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
    METER_SETTLE_POLL,
    METER_SETTLE_TIMEOUT,
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
from .filament import FilamentSlot, bambu_slots, klipper_slots
from .finish import async_run_end_actions
from .firmware import COSMOS_RELEASES_URL, Firmware, firmware_from_system_info
from .identity import KIND_CAMERA_TLS, KIND_MACHINE, KIND_SSH, KIND_TLS, PrinterIdentity
from .models import ElectricityAccumulator, JobPhase, PrintJob, PrintResult, filament_price
from .naming import bambu_model
from .outcome import MARKABLE, mark_history
from .sheets import CORRECTION_KEY, SheetsLog, correction_note, mark_pending_row, queue_row, sheet_row
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

# Results of a power off that left the plug on (the card says why until the plug is switched again).
# no_contact: the printer did not answer; identity: it does not look like the same printer;
# print_open: a print is still open (never finished).
LEFT_ON_RESULTS: Final = frozenset({"hot", "still_on", "no_shutdown", "no_contact", "identity", "print_open"})
# Seconds to read Moonraker's history or a file's metadata when a print ends.
FILAMENT_READ_TIMEOUT: Final = 15


def _remark(job: PrintJob, text: str) -> None:
    """Add a note to the print once."""
    if text not in job.remarks:
        job.remarks.append(text)


class PowerRefused(HomeAssistantError):
    """Safe power off refused: the plug stays on. ``result`` says why (see LEFT_ON_RESULTS)."""

    def __init__(self, result: str, message: str) -> None:
        super().__init__(message)
        self.result = result


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
# ha-bambulab chamber light (shown on the card; this integration does not make a second one)
_BAMBU_LIGHT_KEYS: tuple[str, ...] = ("chamber_light",)
# Bambu LAN: the one light this integration switches itself
BAMBU_LAN_LIGHT = "light_chamber"

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


@callback
def own_device(registry: dr.DeviceRegistry, entry_id: str) -> dr.DeviceEntry | None:
    """The device of this config entry (identifier ``(DOMAIN, entry_id)``).

    HA 2026.10 deprecated ``async_get_device`` (it stops working in 2027.8) in favour of
    ``async_get_device_by_identifier``, which older HA versions do not have.
    """
    identifier = (DOMAIN, entry_id)
    if (by_identifier := getattr(registry, "async_get_device_by_identifier", None)) is not None:
        return by_identifier(identifier, entry_id)
    return registry.async_get_device(identifiers={identifier})


class PrintCostCoordinator(DataUpdateCoordinator[PrintCostData]):
    """One coordinator per configured printer."""

    config_entry: PrintCostConfigEntry

    def __init__(self, hass: HomeAssistant, entry: PrintCostConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} {entry.title}", update_interval=None)
        self.conf: dict[str, Any] = {**entry.data, **entry.options}
        # The entry title is the name the user sees and can rename in Home Assistant; the stored
        # name may still be the hostname Moonraker reported when the printer was added.
        self.printer_name: str = entry.title or self.conf.get(CONF_PRINTER_NAME) or "Printer"
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
                check_identity=self._check_bambu_identity,
            )
        # Bambu LAN camera: A1 / P1 send JPEGs on port 6000 (X1 / H2 use RTSP: not supported yet).
        self.bambu_camera: BambuCamera | None = None
        if self.bambu_lan is not None and has_jpeg_camera(bambu_model(self.conf.get(CONF_PRINTER_SERIAL))):
            self.bambu_camera = BambuCamera(
                self.conf[CONF_HOST],
                self.conf[CONF_ACCESS_CODE],
                lambda coro: entry.async_create_background_task(hass, coro, f"{DOMAIN}_bambu_camera"),
                check_identity=self._check_bambu_camera_identity,
            )
        self._last_bambu_process: float = 0.0
        self._last_bambu_state: str | None = None
        self._polling = False
        self._last_snapshot: PrinterSnapshot | None = None
        # Safe power: "on" / "off" follow the plug; "cooling" while the nozzle cools down (also right after a
        # successful print, with auto-off on), then "shutting_down" until the computer is off.
        self.power_phase: str | None = None
        self.power_cut_at: datetime | None = None
        # How the last power off ended: power_off, aborted, hot (nozzle stayed hot), still_on (computer kept running)
        self.power_result: str | None = None
        self._power_task: asyncio.Task | None = None
        # Firmware reported by Moonraker (Klipper only; None until read).
        self.firmware: Firmware | None = None
        # Klipper camera found through Moonraker (None: no camera, or not checked yet).
        self.webcam: Webcam | None = None
        self._webcam_checked = False
        # Colour (or pin value) of each Klipper light when it was turned off, to turn it back on as it was.
        self._light_last: dict[str, tuple[float, ...]] = {}

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
        # One row per finished print in a Google Sheet (optional)
        self.sheets = SheetsLog(hass, entry.entry_id, self.printer_name, self.conf)

        # Who the printer is, learnt at first contact (identity.py); loaded from storage at start.
        self.identity = PrinterIdentity()
        self._identity_due = True  # Klipper: read the machine identity at the next good poll

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
        self.identity = PrinterIdentity(self.state.identity)

        keys = (CONF_SWITCH_ENTITY, CONF_POWER_ENTITY, CONF_ENERGY_ENTITY, CONF_PRICE_ENTITY)
        if self.printer_type is PrinterType.BAMBU:
            keys = (*BAMBU_ENTITY_KEYS, *keys)
        watched = [self.conf[k] for k in keys if self.conf.get(k)]
        if watched:
            self._unsubs.append(async_track_state_change_event(self.hass, watched, self._on_state_change))
        if self.klipper is not None:
            await self.klipper.async_update()
            await self.async_check_klipper_identity()
            self._identity_due = not self.klipper.available
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
        # Rows that could not reach Google Sheets before; at start the integration may still be loading.
        if self.state.sheets_pending:
            self._unsubs.append(async_at_started(self.hass, self._on_started_send_rows))

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
        if self.identity.changed:  # not the printer we know: no commands
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
            await self._async_bambu_lan_send(
                action, self.bambu_lan.async_command({"pause": "pause", "resume": "resume", "cancel": "stop"}[action])
            )
            return
        button = self._bambu_button(_BAMBU_BUTTONS[action])
        await self.hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)

    async def async_power_on(self) -> None:
        """Turn the plug on (cancels a pending auto-off or safe shutdown)."""
        if not self.switch_entity:
            raise HomeAssistantError(f"{self.printer_name} has no smart plug")
        if self._power_task is not None:
            self._power_task.cancel()
        self.power_result = None  # a new power action: the last "not turned off" no longer applies
        await self.hass.services.async_call(
            "homeassistant", "turn_on", {"entity_id": self.switch_entity}, blocking=True
        )

    @property
    def auto_off(self) -> bool:
        """Turn the printer off safely as soon as a print ends well (0.12.3: no timer, it waits for the nozzle)."""
        return bool(self.conf.get(CONF_AUTO_OFF, DEFAULT_AUTO_OFF))

    @property
    def plug_is_on(self) -> bool:
        """The smart plug is on."""
        state = self.hass.states.get(self.switch_entity) if self.switch_entity else None
        return state is not None and state.state == STATE_ON

    @callback
    def _start_auto_off(self, job: PrintJob) -> None:
        """A print ended well: start the safe power off now (it waits for the nozzle to drop below 80 °C).

        Only after a successful print. Failed, cancelled, interrupted or unknown:
        the printer stays on so it can be looked at.
        """
        if job.result is not PrintResult.SUCCESS:
            _LOGGER.info("%s: print ended as %s, no auto-off", self.printer_name, job.result)
            return
        if not self.auto_off or self.power_phase is not None or self._power_task is not None or not self.plug_is_on:
            return
        _LOGGER.info("%s: print ended well, turning the printer off once the nozzle is cool", self.printer_name)
        self.power_result = None
        task = self.config_entry.async_create_background_task(self.hass, self._async_auto_off(), f"{DOMAIN}_auto_off")
        # Tasks start eagerly: by now the power off may already have stored its own cooling task. Never
        # overwrite it, or "turned on by hand" / "a new print" would cancel the wrong task.
        if self._power_task is None and not task.done():
            self._power_task = task

    @callback
    def _check_auto_off(self) -> None:
        """While the nozzle cools: a new print or the plug turned off by hand cancels the power off.

        Losing contact or another printer answering are handled by the cooling loop and
        ``_on_identity_changed``: the plug is left on.
        """
        if self.power_phase != "cooling" or self._power_task is None:
            return
        if self.is_printing:
            self._cancel_cooling("a new print started")
        elif not self.plug_is_on:
            self._cancel_cooling("the plug was turned off")

    @callback
    def _cancel_cooling(self, why: str, result: str = "aborted") -> None:
        if self._power_task is not None:
            self._power_task.cancel()
        self._end_power_off(result, why)

    async def _async_auto_off(self) -> None:
        """The usual safe power off (cool, shut the computer down, cut once it is off)."""
        self._power_task = None
        try:
            await self.async_power_off()
        except PowerRefused as err:
            self._end_power_off(err.result, str(err))
        except HomeAssistantError as err:
            self._end_power_off("aborted", str(err))

    @callback
    def _end_power_off(self, result: str, why: str) -> None:
        _LOGGER.info("%s: power off %s (%s)", self.printer_name, result, why)
        self.power_result = result
        self.power_phase = None
        self.power_cut_at = None
        self._power_task = None
        self._publish(dt_util.utcnow(), self.reader.price_per_kwh())

    def power_blocker(self) -> PowerRefused | None:
        """Why the plug must not be cut now: another printer, no contact, a print still open. ``None``: it may."""
        if self.identity.changed:
            return PowerRefused(
                "identity",
                f"{self.printer_name}: this does not look like the same printer ({self.identity.describe()} "
                "changed) — power not cut. Check the printer, then accept it in its options.",
            )
        if not self.reachable:
            return PowerRefused(
                "no_contact",
                f"{self.printer_name}: no contact with the printer — power not cut. "
                "Use the plug directly if you are sure.",
            )
        if self.tracker.active_job is not None:
            return PowerRefused("print_open", f"{self.printer_name}: a print is still open — power not cut.")
        return None

    @callback
    def _power_left_on(self, refused: PowerRefused, *, notify: bool) -> None:
        """The plug stays on: remember why (the card shows it) and, for a background power off, say so."""
        _LOGGER.warning("%s", refused)
        self.power_result = refused.result
        if notify:
            persistent_notification.async_create(
                self.hass,
                str(refused),
                title="FDM Printer Manager",
                notification_id=f"{DOMAIN}_not_off_{self.config_entry.entry_id}",
            )
        self._publish(dt_util.utcnow(), self.reader.price_per_kwh())

    async def async_power_off(self) -> None:
        """Safe power off: never while printing, never with a hot nozzle, never without contact with the printer.

        Klipper shuts its computer down first. A printer that does not answer
        (offline, Moonraker or MQTT down, state unknown) or does not look like
        the same printer is refused with :class:`PowerRefused`: the plug stays on.
        """
        if not self.switch_entity:
            raise HomeAssistantError(f"{self.printer_name} has no smart plug")
        if self.is_printing:
            raise HomeAssistantError(f"{self.printer_name} is printing: it will not be turned off")
        if self.power_phase is not None:  # already cooling or shutting down
            return
        await self.async_check_klipper_identity()
        if (refused := self.power_blocker()) is not None:
            self._power_left_on(refused, notify=False)
            raise refused
        self.power_result = None  # a new power off: forget why the last one left the plug on
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
        """Nozzle at or above SAFE_POWER_COOL_TEMP. No reading counts as cool: callers check contact first."""
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
                if (refused := self.power_blocker()) is not None:
                    # Lost the printer while it cooled: no reading is not "cool", the plug stays on.
                    self._power_left_on(refused, notify=True)
                    return
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

        Only while the printer answers and is the same printer. Moonraker's
        ``machine.shutdown`` first; if Moonraker refuses, the SSH login (its host
        key checked). The plug is then cut only once the computer stops answering.
        """
        if (refused := self.power_blocker()) is not None:
            self._power_left_on(refused, notify=True)
            return
        if self.klipper is None:
            await self._async_cut_plug()
            return
        host = self.conf[CONF_HOST]
        sent = False
        moonraker_error: str | None = None
        try:
            await self.klipper.client.shutdown_host()
            sent = True
        except Exception as err:  # noqa: BLE001 - SSH decides
            moonraker_error = str(err)
            _LOGGER.warning("%s: Moonraker shutdown request failed: %s", self.printer_name, err)
        if not sent and (login := await self._async_shutdown_login(moonraker_error)) is not None:
            try:
                key = await ssh_shutdown(host, login, host_key=self.identity.known.get(KIND_SSH))
                sent = True
                if isinstance(key, str):
                    self._learn_identity(KIND_SSH, key)
            except SshHostKeyChanged as err:
                _LOGGER.warning("%s: %s, nothing sent", self.printer_name, err)
                self.identity.refuse(KIND_SSH)
                self._on_identity_changed()
                self._power_left_on(self.power_blocker() or PowerRefused("identity", str(err)), notify=True)
                return
            except SshError as err:
                _LOGGER.warning("%s: SSH shutdown failed: %s", self.printer_name, err)
        if not sent:
            # Moonraker cannot shut down (the Elegoo Centauri Carbon replies HTTP 500
            # "Error running shell command") and SSH failed or is not set up:
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

        Here, and only here, the printer going silent is what is waited for: it
        has just accepted the shutdown itself (Moonraker or SSH). Moonraker stops
        early in the shutdown, so the computer itself is checked (ping, or a TCP
        connection to it). Still answering after SAFE_POWER_WAIT: the plug is
        left on and a notification says so.
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
        for task in list(self._tasks):
            task.cancel()
        if self.bambu_lan is not None:
            await self.bambu_lan.stop()
        if self.bambu_camera is not None:
            await self.bambu_camera.stop()
        self._sync_state()
        await self.store.async_save(self.state)

    async def _on_started_send_rows(self, _hass: HomeAssistant) -> None:
        self._send_sheet_rows()

    def _send_sheet_rows(self) -> None:
        """Send the queued Google Sheets rows in the background."""
        if self.sheets.enabled and self.state.sheets_pending:
            self._spawn(self.sheets.async_flush(self.state.sheets_pending, lambda: self.store.async_save(self.state)))

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
        row = next((h for h in self.state.history if h.get("job_id") == job_id), None)
        before = row.get("result") if row else (str(last.result) if last and last.job_id == job_id else None)
        found = mark_history(self.state.history, job_id, result)
        if last is not None and last.job_id == job_id:
            last.result = PrintResult(result)
            last.result_by = "user"
            found = True
        if not found:
            raise HomeAssistantError(f"{self.printer_name}: print {job_id} not found")
        # Google Sheets: a row still in the queue is changed there; else the row already in the sheet is changed
        # in place when it is sent (found by job_id), or a correction row is added when it cannot be (0.12.3).
        if self.sheets.enabled and before != result and not mark_pending_row(self.state.sheets_pending, job_id, result):
            summary = row or (last.summary() if last else {})
            queue_row(
                self.state.sheets_pending,
                {
                    **sheet_row(
                        {"printer": self.printer_name, "currency": self.currency, **summary, "result_by": "user"},
                        dt_util.get_default_time_zone(),
                        correction=correction_note(result, before),
                    ),
                    CORRECTION_KEY: True,
                },
            )
        await self.store.async_save(self.state)
        self.hass.bus.async_fire(
            EVENT_PRINT_MARKED,
            {"entry_id": self.config_entry.entry_id, "printer": self.printer_name, "job_id": job_id, "result": result},
        )
        _LOGGER.info("%s: print %s marked as %s", self.printer_name, job_id, result)
        self._send_sheet_rows()
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
            self._identity_due = True  # and check it is still the same printer
        else:
            if self._identity_due:
                self._identity_due = False
                self._spawn(self.async_check_klipper_identity())
            if not self._webcam_checked:
                self._spawn(self.async_check_webcam())
        self._process(dt_util.utcnow())

    async def _async_webcam_tick(self, now: datetime) -> None:
        await self.async_check_klipper_identity()
        await self.async_check_webcam()

    async def async_check_webcam(self) -> None:
        """Look for an enabled camera on the Klipper printer: at start, when it comes back, every 30 minutes."""
        if self.klipper is None or not self.klipper.available:
            return
        self._webcam_checked = True
        try:
            web_url = self.configuration_url or default_web_url(self.conf[CONF_HOST])
            cams = await self.klipper.client.webcams(web_url, self.camera_hosts)
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
    def camera_hosts(self) -> frozenset[str]:
        """Hosts camera URLs may point to: the printer and its web interface, nothing else."""
        hosts = {str(self.conf.get(CONF_HOST) or "").strip().strip("[]").lower()}
        if (web := url_host(self.configuration_url)) is not None:
            hosts.add(web)
        return frozenset(h for h in hosts if h)

    # ------------------------------------------------------------------ #
    # Is it still the same printer? (identity.py)
    # ------------------------------------------------------------------ #
    async def async_check_klipper_identity(self) -> None:
        """Klipper: read who the computer is and compare it with the one remembered."""
        if self.klipper is None or not self.klipper.available:
            return
        try:
            seen = await self.klipper.client.machine_identity()
        except Exception as err:  # noqa: BLE001 - no reading is no verdict
            _LOGGER.debug("%s: machine identity not readable: %s", self.printer_name, err)
            return
        if self._learn_identity(KIND_MACHINE, seen if isinstance(seen, str) else None):
            self.async_firmware_seen(getattr(self.klipper.client, "system_info", None))

    # ------------------------------------------------------------------ #
    # Firmware (firmware.py): Cosmos before 26.10 cannot shut down from Moonraker
    # ------------------------------------------------------------------ #
    @property
    def firmware_issue_id(self) -> str:
        """Repair issue recommending a firmware update."""
        return f"firmware_update_{self.config_entry.entry_id}"

    @property
    def firmware_update_recommended(self) -> bool:
        """The card shows "Firmware update recommended"."""
        return self.firmware is not None and self.firmware.update_recommended

    @callback
    def async_firmware_seen(self, system_info: dict[str, Any] | None) -> None:
        """Remember the firmware; recommend an update for Cosmos before 26.10, withdraw it once updated."""
        firmware = firmware_from_system_info(system_info)
        if firmware is None:
            return  # Moonraker did not say: no verdict, change nothing
        changed = firmware != self.firmware
        self.firmware = firmware
        if firmware.update_recommended:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self.firmware_issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                learn_more_url=COSMOS_RELEASES_URL,
                translation_key="cosmos_update",
                translation_placeholders={
                    "name": self.printer_name,
                    "version": firmware.version or "?",
                    "url": COSMOS_RELEASES_URL,
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, self.firmware_issue_id)
        if not changed:
            return
        _LOGGER.info("%s: firmware %s", self.printer_name, firmware.label)
        registry = dr.async_get(self.hass)
        device = own_device(registry, self.config_entry.entry_id)
        if device is not None and device.sw_version != firmware.label:
            registry.async_update_device(device.id, sw_version=firmware.label)
        self._publish(dt_util.utcnow(), self.reader.price_per_kwh())

    @callback
    def _check_bambu_identity(self, fingerprint: str | None) -> bool:
        """Bambu LAN, before logging in: True when this is the TLS certificate seen at first contact."""
        return self._learn_identity(KIND_TLS, fingerprint)

    @callback
    def _check_bambu_camera_identity(self, fingerprint: str | None) -> bool:
        """Bambu LAN camera, before logging in: its own certificate (port 6000), and the printer still the known one."""
        return self._learn_identity(KIND_CAMERA_TLS, fingerprint) and not self.identity.changed

    @callback
    def _learn_identity(self, kind: str, seen: str | None) -> bool:
        """Compare (or remember at first contact) one identity value. False: not the same printer."""
        was_changed = self.identity.changed
        ok, learned = self.identity.check(kind, seen)
        if learned:
            _LOGGER.info("%s: remembered its %s", self.printer_name, kind)
            self.state.identity = dict(self.identity.known)
            self._spawn(self.store.async_save(self.state))
        if not ok:
            self._on_identity_changed()
        elif was_changed and not self.identity.changed:
            _LOGGER.info("%s: the known printer answers again", self.printer_name)
            ir.async_delete_issue(self.hass, DOMAIN, self.identity_issue_id)
            self._publish(dt_util.utcnow(), self.reader.price_per_kwh())
        return ok

    @property
    def identity_issue_id(self) -> str:
        """Repair issue raised when the printer does not look like the same one."""
        return f"identity_changed_{self.config_entry.entry_id}"

    @callback
    def _on_identity_changed(self) -> None:
        """Not the same printer: no commands, no power cut, the admin decides (options flow)."""
        _LOGGER.warning(
            "%s: this does not look like the same printer (%s changed): commands and power off are blocked",
            self.printer_name,
            self.identity.describe(),
        )
        if self.power_phase == "cooling" and self._power_task is not None:
            self._cancel_cooling("it does not look like the same printer", "identity")
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self.identity_issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="identity_changed",
            translation_placeholders={"name": self.printer_name, "what": self.identity.describe()},
        )
        self._publish(dt_util.utcnow(), self.reader.price_per_kwh())

    async def async_accept_identity(self) -> None:
        """The admin checked the printer: remember what answers now as this printer."""
        self.identity.accept()
        self.state.identity = dict(self.identity.known)
        await self.store.async_save(self.state)
        ir.async_delete_issue(self.hass, DOMAIN, self.identity_issue_id)
        _LOGGER.info("%s: new identity accepted by the admin", self.printer_name)
        self._publish(dt_util.utcnow(), self.reader.price_per_kwh())

    @property
    def camera_entity(self) -> str | None:
        """Camera the panel shows: ours for Klipper and Bambu LAN, ha-bambulab's own for Bambu (never a second one)."""
        if self.klipper is not None:
            if self.webcam is None:
                return None
            return er.async_get(self.hass).async_get_entity_id("camera", DOMAIN, f"{self.config_entry.entry_id}_camera")
        if self.printer_type is PrinterType.BAMBU:
            return self._bambu_entity("camera", _BAMBU_CAMERA_KEYS) or self._bambu_entity("image", _BAMBU_CAMERA_KEYS)
        if self.bambu_camera is not None:
            return er.async_get(self.hass).async_get_entity_id("camera", DOMAIN, f"{self.config_entry.entry_id}_camera")
        return None

    # ------------------------------------------------------------------ #
    # Lights, baby steps, filaments (only what the printer really has)
    # ------------------------------------------------------------------ #
    def _klipper_lights(self) -> dict[str, KlipperLight]:
        features = self.klipper.features if self.klipper is not None else None
        return {light.key: light for light in features.lights} if features else {}

    @property
    def own_lights(self) -> dict[str, str]:
        """Lights this integration switches itself, key → name (Klipper LEDs / light pins, Bambu LAN chamber light)."""
        if self.klipper is not None:
            return {key: light.label for key, light in self._klipper_lights().items()}
        if self.bambu_lan is not None and light_on(self.bambu_lan.report) is not None:
            return {BAMBU_LAN_LIGHT: "Chamber light"}
        return {}

    def light_is_on(self, key: str) -> bool | None:
        """On / off as last read, ``None`` when unknown."""
        if self.klipper is not None:
            light = self._klipper_lights().get(key)
            level = light_level(light, self.klipper.reading.extras) if light else None
            return None if level is None else level > 0
        if self.bambu_lan is not None and key == BAMBU_LAN_LIGHT:
            return light_on(self.bambu_lan.report)
        return None

    async def async_set_light(self, key: str, on: bool) -> None:
        """Turn one light on or off on the printer."""
        if self.identity.changed:
            raise HomeAssistantError(f"{self.printer_name}: this does not look like the same printer, nothing sent")
        if self.klipper is not None and (light := self._klipper_lights().get(key)) is not None:
            if not self.klipper.available:
                raise HomeAssistantError(f"{self.printer_name} is not answering")
            if not on and (color := light_color(light, self.klipper.reading.extras)) and any(c > 0 for c in color):
                self._light_last[key] = color
            try:
                await self.klipper.client.gcode(light_gcode(light, on, self._light_last.get(key)))
            except MoonrakerError as err:
                raise HomeAssistantError(f"{self.printer_name}: {err}") from err
            await self.klipper.async_update()
            self._process(dt_util.utcnow())
            return
        if self.bambu_lan is not None and key == BAMBU_LAN_LIGHT:
            await self._async_bambu_lan_send("light", self.bambu_lan.async_light(on))
            return
        raise HomeAssistantError(f"{self.printer_name} has no light {key}")

    async def _async_bambu_lan_send(self, what: str, sending: Any) -> None:
        """Run a Bambu LAN command. Refused → a clear notice (Developer Mode), not an error."""
        try:
            await sending
        except BambuCommandRefused as err:
            message = (
                f"{self.printer_name}: the printer refused the {what} command ({err}). "
                "Sending commands requires Developer Mode on the printer's LAN Only page. "
                "Status, temperatures and safe power work without it."
            )
            _LOGGER.warning(message)
            persistent_notification.async_create(
                self.hass,
                message,
                title="FDM Printer Manager",
                notification_id=f"{DOMAIN}_developer_mode_{self.config_entry.entry_id}",
            )
        except BambuError as err:
            raise HomeAssistantError(f"{self.printer_name}: {err}") from err

    def light_entities(self) -> list[dict[str, Any]]:
        """Lights for the card: ours, or ha-bambulab's chamber light."""
        registry = er.async_get(self.hass)
        lights = [
            {"key": key, "name": name, "entity": entity}
            for key, name in self.own_lights.items()
            if (entity := registry.async_get_entity_id("light", DOMAIN, f"{self.config_entry.entry_id}_{key}"))
        ]
        if self.printer_type is PrinterType.BAMBU and (entity := self._bambu_entity("light", _BAMBU_LIGHT_KEYS)):
            lights.append({"key": "light_chamber", "name": "Chamber light", "entity": entity})
        return lights

    @property
    def z_offset(self) -> float | None:
        """Live Z offset of a Klipper printer (mm), when it can be baby-stepped."""
        features = self.klipper.features if self.klipper is not None else None
        if not features or not features.babystep or not self.klipper.available:
            return None
        return z_offset(self.klipper.reading.extras)

    @property
    def babystep_enabled(self) -> bool:
        """Baby steps are offered for this printer (Klipper knows SET_GCODE_OFFSET and the option is on)."""
        features = self.klipper.features if self.klipper is not None else None
        return bool(features and features.babystep and self.conf.get(CONF_BABYSTEP, True))

    @property
    def babystep_save(self) -> str | None:
        """The command that keeps the offset, when the user turned "save" on."""
        features = self.klipper.features if self.klipper is not None else None
        if not features or not self.conf.get(CONF_BABYSTEP_SAVE, False):
            return None
        return features.save_gcode

    async def async_babystep(self, delta: float | None = None, *, reset: bool = False, save: bool = False) -> None:
        """Move the nozzle up / down while printing, reset the offset, or keep it (Z_OFFSET_APPLY_*)."""
        if not self.babystep_enabled or self.klipper is None or not self.klipper.available:
            raise HomeAssistantError(f"{self.printer_name}: baby steps are not available")
        if self.identity.changed:
            raise HomeAssistantError(f"{self.printer_name}: this does not look like the same printer, nothing sent")
        if save:
            if not (command := self.babystep_save):
                raise HomeAssistantError(f"{self.printer_name}: saving the Z offset is turned off in the options")
            script = command
        elif not self.is_printing:
            raise HomeAssistantError(f"{self.printer_name}: baby steps only while printing")
        elif reset:
            script = BABYSTEP_RESET
        else:
            try:
                # Like Mainsail / Fluidd: Z_ADJUST, never blocked because the offset is not read yet.
                script = babystep_gcode(float(delta or 0), self.z_offset)
            except ValueError as err:
                raise HomeAssistantError(f"{self.printer_name}: {err}") from err
        try:
            await self.klipper.client.gcode(script)
        except MoonrakerError as err:
            raise HomeAssistantError(f"{self.printer_name}: {err}") from err
        if save:
            persistent_notification.async_create(
                self.hass,
                f"{self.printer_name}: Z offset applied ({script}). Run SAVE_CONFIG when the printer is idle "
                "to keep it (it restarts Klipper).",
                title="FDM Printer Manager",
                notification_id=f"{DOMAIN}_zsave_{self.config_entry.entry_id}",
            )
        await self.klipper.async_update()
        self._process(dt_util.utcnow())

    @property
    def filament_slots(self) -> list[FilamentSlot]:
        """Filaments of a multi-material unit (AFC, Happy Hare, AMS), or Spoolman's active spool."""
        if self.klipper is not None and self.klipper.available:
            return klipper_slots(self.klipper.reading.extras, self.klipper.spool)
        if self.bambu_lan is not None:
            return bambu_slots(self.bambu_lan.report)
        return []

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
            web_url = self.conf.get(CONF_WEB_URL)
            # Only http(s): a link like javascript:... is never handed to the panel or the device page.
            return web_url if url_host(web_url) else default_web_url(self.conf[CONF_HOST])
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
            meta = self.klipper.file_meta
            return PrinterSnapshot(
                timestamp=now,
                status=reading.state,
                electricity=self.reader.sample(now),
                job_name=reading.filename,
                progress=reading.progress,
                remaining_min=self.klipper.remaining_min,
                filament_mm=reading.filament_mm,
                filament_g_per_mm=meta.filament_g_per_mm if meta else None,
                filament_type=meta.filament_type if meta else None,
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
                self._start_auto_off(job)
        self._check_auto_off()

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
                **self._counter_values(),
            )
        )

    def _counter_values(self) -> dict[str, Any]:
        """The printer's counters as shown: they never go down (see ElectricityAccumulator.reported_energy_kwh)."""
        total, today = self.state.total, self.state.today
        return {
            "energy_total_kwh": total.reported_energy_kwh if total else None,
            "cost_total": self._counter_cost(total),
            "energy_today_kwh": today.reported_energy_kwh if today else None,
            "cost_today": self._counter_cost(today),
        }

    @callback
    def restore_counter(self, key: str, value: Any, shown_at: datetime) -> None:
        """A counter sensor's last value before the restart: never show less than it.

        Covers a stored file older than what the sensor last showed (HA stopped
        without its final save). "Today" values only count for the same day:
        after midnight the day counter starts again from zero.
        """
        if not self.reader.sources.measures_energy:
            return
        try:
            number = Decimal(str(value))
        except (ArithmeticError, ValueError):
            return
        if not number.is_finite() or number < 0:
            return
        state = self.state
        if key.endswith("_today"):
            day = dt_util.as_local(shown_at).date().isoformat()
            if day != dt_util.as_local(dt_util.utcnow()).date().isoformat():
                return
            if state.today is None or state.today_date != day:
                state.today = self._new_counter()
                state.today_date = day
            acc = state.today
        else:
            if state.total is None:
                state.total = self._new_counter()
            acc = state.total
        if key.startswith("energy"):
            acc.raise_floor(energy_kwh=float(number))
        else:
            acc.raise_floor(cost=number)
        if self.data is not None:
            self.data = replace(self.data, **self._counter_values())
            self.async_update_listeners()
        self.store.async_delay_save(self.state)

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
        if acc.reported_cost == 0 and acc.total_uncosted_energy_kwh > 0:
            return None
        return acc.reported_cost

    # ------------------------------------------------------------------ #
    # End of a print
    # ------------------------------------------------------------------ #
    async def _async_finalize(self, job: PrintJob, *, capture_image: bool = False) -> None:
        """Store the finished job as the last print and announce it.

        The thumbnail is read right away: after a restart the cover image may
        already show another job, so a resumed finalisation keeps none.
        """
        image = await self._async_read_cover_image() if capture_image else None
        if job.electricity.awaiting_meter:
            await self._async_wait_meter_step(job)
            job.electricity.flush()
        await self._async_filament_end(job)
        job.phase = JobPhase.COMPLETED
        if job in self.state.finalizing:
            self.state.finalizing.remove(job)
        self.state.last_job = job
        self.last_image = image
        summary = job.summary()
        self.state.history.insert(0, {"printer": self.printer_name, **summary})
        del self.state.history[HISTORY_LIMIT:]
        payload = {"printer": self.printer_name, "currency": self.currency, **summary}
        # Queued with the same save as the history: exactly one row per print, kept if Sheets cannot be reached.
        if self.sheets.enabled:
            queue_row(self.state.sheets_pending, sheet_row(payload, dt_util.get_default_time_zone()))
        await self.store.async_save(self.state)
        await self.store.async_save_image(image)

        self.hass.bus.async_fire(EVENT_PRINT_FINISHED, {"entry_id": self.config_entry.entry_id, **payload})
        for listener in list(self._finished_listeners):
            listener(payload)
        await async_run_end_actions(self.hass, self.conf, payload)
        self._send_sheet_rows()
        _LOGGER.info(
            "Print '%s' finished: result=%s energy=%s kWh cost=%s %s",
            job.name,
            job.result,
            summary["energy_kwh"],
            summary["electricity_cost"],
            self.currency,
        )
        self._publish(dt_util.utcnow(), self.reader.price_per_kwh())

    async def _async_filament_end(self, job: PrintJob) -> None:
        """The filament of the finished print, whatever its result: final value, price, and what is unknown."""
        if self.klipper is not None and job.name:
            await self._async_klipper_filament_end(job)
        price = filament_price(self.conf.get(CONF_FILAMENT_PRICE))
        if price is not None:
            job.filament_price_per_kg = price
        if job.filament_mm is None:
            _remark(
                job,
                "Filament used unknown: the printer did not report it"
                if self.klipper is not None
                else "Filament used unknown: Bambu Lab printers do not report the filament a print used",
            )
        elif job.filament_g_per_mm is None:
            _remark(job, "Filament weight unknown: the slicer did not write the filament weight in the file")

    async def _async_klipper_filament_end(self, job: PrintJob) -> None:
        """Klipper: the final length from Moonraker's history, and the file's grams per mm if still missing.

        The history only ever raises what was seen during the print: after a
        Klipper restart or disconnection it says 0 mm, and the length seen is kept.
        """
        client = self.klipper.client
        seen = job.filament_seen_mm
        try:
            async with asyncio.timeout(FILAMENT_READ_TIMEOUT):
                jobs = await client.history()
        except Exception as err:  # noqa: BLE001 - the history only completes what was seen
            _LOGGER.debug("%s: Moonraker history not readable: %s", self.printer_name, err)
            jobs = None
        final = history_filament(
            jobs,
            job.name,
            job.started_at.timestamp(),
            job.finished_at.timestamp() if job.finished_at else None,
        )
        job.record_final_filament(final)
        if seen is not None and seen > 0 and final is None and isinstance(jobs, list) and jobs:
            _remark(job, "Filament used as seen during the print: Moonraker's history has no length for it")
        if job.filament_g_per_mm is None:
            try:
                async with asyncio.timeout(FILAMENT_READ_TIMEOUT):
                    meta = await client.metadata(job.name)
            except Exception as err:  # noqa: BLE001 - the weight stays unknown, with a note
                _LOGGER.debug("%s: no metadata for %s: %s", self.printer_name, job.name, err)
                meta = None
            if isinstance(meta, FileMetadata):
                job.set_filament_file(meta.filament_g_per_mm, meta.filament_type)

    async def _async_wait_meter_step(self, job: PrintJob) -> None:
        """Wait for the plug's energy meter to step, so the end of the print is measured, not estimated.

        A plug counts in small steps every one or two minutes: when the printer
        says "finished", the energy since the last step is not in the meter yet.
        The next step (or ``METER_SETTLE_TIMEOUT``) closes the account. The job
        stays in ``finalizing`` (saved), so a restart resumes the wait.
        """
        acc = job.electricity
        deadline = dt_util.utcnow() + timedelta(seconds=METER_SETTLE_TIMEOUT)
        while dt_util.utcnow() < deadline:
            sample = self.reader.sample(dt_util.utcnow())
            if acc.meter_stepped(sample.energy_kwh):
                acc.add_sample(sample)
                _LOGGER.debug("Print '%s': energy meter stepped, end of the print measured", job.name)
                return
            await asyncio.sleep(METER_SETTLE_POLL)
        _LOGGER.info("Print '%s': energy meter did not step within %s s", job.name, METER_SETTLE_TIMEOUT)

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
