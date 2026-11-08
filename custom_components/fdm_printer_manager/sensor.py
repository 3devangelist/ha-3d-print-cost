"""Sensors. Pure presentation: values come from the PrintJob model."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import filament
from .adapters.klipper_extras import BABYSTEP_LIMIT, BABYSTEP_STEPS
from .const import (
    CONF_COVER_IMAGE_ENTITY,
    CONF_ENERGY_ENTITY,
    CONF_HOST,
    CONF_POWER_ENTITY,
    CONF_PRICE_ENTITY,
    CONF_PRICE_FIXED,
    CONF_SWITCH_ENTITY,
    DOMAIN,
)
from .coordinator import PrintCostConfigEntry, PrintCostCoordinator, PrintCostData
from .entity import PrintCostEntity
from .models import PrintJob, PrintResult

PARALLEL_UPDATES = 0

CURRENCY = "__currency__"  # replaced by the configured currency
CURRENCY_PER_HOUR = "__currency__/h"
CURRENCY_PER_KWH = "__currency__/kWh"

# The printer's counters: never below the value shown before a restart.
COUNTER_KEYS = frozenset({"energy_total", "cost_total", "energy_today", "cost_today"})


@dataclass(frozen=True, kw_only=True)
class PrintCostSensorDescription(SensorEntityDescription):
    """Describes a sensor with value and attribute getters."""

    value_fn: Callable[[PrintCostData], Any]
    attrs_fn: Callable[[PrintCostData], dict[str, Any] | None] | None = None


def _current(data: PrintCostData) -> PrintJob | None:
    return data.current_job


def _money(value: Decimal | None) -> Decimal | None:
    # Full precision is kept internally; HA rounds for display via
    # suggested_display_precision.
    return None if value is None else round(value, 6)


def _current_attrs(data: PrintCostData) -> dict[str, Any] | None:
    job = data.current_job
    if job is None:
        return None
    summary = job.summary(data.now)
    return {
        "job_name": job.name,
        "status": job.printer_state,
        "phase": str(job.phase),
        "progress": job.progress,
        "remaining_min": job.remaining_min,
        "started_at": summary["started_at"],
        "energy_kwh": summary["energy_kwh"],
        "electricity_cost_complete": summary["electricity_cost_complete"],
        "notes": summary["notes"],
    }


def _last_attrs(data: PrintCostData) -> dict[str, Any] | None:
    job = data.last_job
    if job is None:
        return None
    summary = job.summary()
    return {k: v for k, v in summary.items() if k != "job_id"}


def _last_filament_attrs(data: PrintCostData) -> dict[str, Any] | None:
    job = data.last_job
    if job is None:
        return None
    summary = job.summary()
    keys = (
        "filament_g",
        "filament_type",
        "filament_type_source",
        "filament_cost",
        "filament_price_per_kg",
        "filament_price_source",
        "electricity_cost",
        "print_cost",
    )
    return {key: summary[key] for key in keys}


SENSORS: tuple[PrintCostSensorDescription, ...] = (
    # ---------------- live printer ----------------
    PrintCostSensorDescription(
        key="printer_status",
        value_fn=lambda d: d.printer_status,
    ),
    PrintCostSensorDescription(
        key="print_progress",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
        value_fn=lambda d: d.progress,
    ),
    PrintCostSensorDescription(
        key="print_file",
        value_fn=lambda d: d.filename,
    ),
    PrintCostSensorDescription(
        key="print_time_left",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        value_fn=lambda d: round(d.remaining_min) if d.remaining_min is not None else None,
    ),
    PrintCostSensorDescription(
        key="nozzle_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda d: d.nozzle_temp,
    ),
    # ---------------- the printer's own counters ----------------
    PrintCostSensorDescription(
        key="energy_total",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda d: round(d.energy_total_kwh, 6) if d.energy_total_kwh is not None else None,
    ),
    PrintCostSensorDescription(
        key="cost_total",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        value_fn=lambda d: _money(d.cost_total),
    ),
    PrintCostSensorDescription(
        key="energy_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        value_fn=lambda d: round(d.energy_today_kwh, 6) if d.energy_today_kwh is not None else None,
    ),
    PrintCostSensorDescription(
        key="cost_today",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        value_fn=lambda d: _money(d.cost_today),
    ),
    # ---------------- current print ----------------
    PrintCostSensorDescription(
        key="current_print_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=3,
        value_fn=lambda d: _money(j.electricity_cost) if (j := _current(d)) else None,
        attrs_fn=_current_attrs,
    ),
    PrintCostSensorDescription(
        key="current_print_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        value_fn=lambda d: round(j.energy_kwh, 6) if (j := _current(d)) else None,
    ),
    PrintCostSensorDescription(
        key="current_print_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=2,
        value_fn=lambda d: round(j.duration_s(d.now)) if (j := _current(d)) else None,
    ),
    PrintCostSensorDescription(
        key="current_print_cost_per_hour",
        native_unit_of_measurement=CURRENCY_PER_HOUR,
        suggested_display_precision=3,
        value_fn=lambda d: _money(j.cost_per_hour(d.now)) if (j := _current(d)) else None,
    ),
    PrintCostSensorDescription(
        key="current_print_phase",
        device_class=SensorDeviceClass.ENUM,
        options=["idle", "active", "finalizing"],
        value_fn=lambda d: str(j.phase) if (j := _current(d)) else "idle",
    ),
    PrintCostSensorDescription(
        key="electricity_price",
        native_unit_of_measurement=CURRENCY_PER_KWH,
        suggested_display_precision=4,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.price_per_kwh,
    ),
    # ---------------- last print ----------------
    PrintCostSensorDescription(
        key="last_print_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=3,
        value_fn=lambda d: _money(d.last_job.electricity_cost) if d.last_job else None,
        attrs_fn=_last_attrs,
    ),
    PrintCostSensorDescription(
        key="last_print_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        value_fn=lambda d: round(d.last_job.energy_kwh, 6) if d.last_job else None,
    ),
    PrintCostSensorDescription(
        key="last_print_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=2,
        value_fn=lambda d: round(d.last_job.duration_s()) if d.last_job else None,
    ),
    PrintCostSensorDescription(
        # 0.15.0: metres of filament of the last print; grams, material and costs in the attributes.
        key="last_print_filament",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS,
        suggested_display_precision=1,
        value_fn=lambda d: (
            round(d.last_job.filament_mm / 1000, 3) if d.last_job and d.last_job.filament_mm is not None else None
        ),
        attrs_fn=_last_filament_attrs,
    ),
    PrintCostSensorDescription(
        key="last_print_result",
        device_class=SensorDeviceClass.ENUM,
        options=[str(r) for r in PrintResult],
        value_fn=lambda d: str(d.last_job.result) if d.last_job and d.last_job.result else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrintCostConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the sensors; the filament and Z offset ones when the printer turns out to have them."""
    coordinator = entry.runtime_data
    async_add_entities(
        (CounterSensor if description.key in COUNTER_KEYS else PrintCostSensor)(coordinator, description)
        for description in SENSORS
    )
    added: set[str] = set()

    @callback
    def _add_extras() -> None:
        new: list[SensorEntity] = []
        if "filaments" not in added and coordinator.filament_slots:
            new.append(FilamentSensor(coordinator))
        if "z_offset" not in added and coordinator.babystep_enabled:
            new.append(ZOffsetSensor(coordinator))
        if new:
            added.update(entity.key for entity in new)
            async_add_entities(new)

    _add_extras()
    entry.async_on_unload(coordinator.async_add_listener(_add_extras))


class FilamentSensor(PrintCostEntity, SensorEntity):
    """Filaments of the multi-material unit: the active material; every slot in the attributes."""

    key = "filaments"

    def __init__(self, coordinator: PrintCostCoordinator) -> None:
        super().__init__(coordinator, self.key, "sensor")

    @property
    def native_value(self) -> str | None:
        """Active material, else "2/4 loaded"."""
        return filament.summary(self.coordinator.filament_slots)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Every slot: name, material, colour, % left, active, empty."""
        return {"slots": [slot.as_dict() for slot in self.coordinator.filament_slots]}


class ZOffsetSensor(PrintCostEntity, SensorEntity):
    """Live Z offset of a Klipper printer (moved by the baby steps)."""

    key = "z_offset"
    _attr_native_unit_of_measurement = UnitOfLength.MILLIMETERS
    _attr_suggested_display_precision = 3
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: PrintCostCoordinator) -> None:
        super().__init__(coordinator, self.key, "sensor")

    @property
    def native_value(self) -> float | None:
        """mm; unknown while the printer does not answer."""
        return self.coordinator.z_offset


class PrintCostSensor(PrintCostEntity, SensorEntity):
    """A sensor reading one value of the current / last print."""

    entity_description: PrintCostSensorDescription

    def __init__(self, coordinator: PrintCostCoordinator, description: PrintCostSensorDescription) -> None:
        super().__init__(coordinator, description.key, "sensor")
        self.entity_description = description
        unit = description.native_unit_of_measurement
        if unit and "__currency__" in unit:
            self._attr_native_unit_of_measurement = unit.replace("__currency__", coordinator.currency)

    @property
    def native_value(self) -> Any:
        """Current value."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def entity_picture(self) -> str | None:
        """Print thumbnail on the cost sensors, shown by cards instead of the icon (Bambu Lab).

        Current print: the ha-bambulab cover image. Last print: the copy kept
        by ``image.<printer>_last_print_image``. The URL carries an access
        token that HA rotates; the periodic update refreshes it.
        """
        data = self.coordinator.data
        if data is None:
            return None
        key = self.entity_description.key
        image: str | None = None
        if key == "current_print_cost" and data.current_job is not None:
            image = self.coordinator.conf.get(CONF_COVER_IMAGE_ENTITY)
        elif key == "last_print_cost" and self.coordinator.last_image is not None:
            image = er.async_get(self.hass).async_get_entity_id(
                "image", DOMAIN, f"{self.coordinator.config_entry.entry_id}_last_print_image"
            )
        state = self.hass.states.get(image) if image else None
        return state.attributes.get("entity_picture") if state else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Compact attributes (no large payloads)."""
        if self.entity_description.key == "printer_status":
            return self._printer_attrs()
        if self.coordinator.data is None or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)

    @property
    def _source(self):
        return self.coordinator.klipper or self.coordinator.bambu_lan

    def _babystep_attrs(self) -> dict[str, Any] | None:
        """Z offset buttons on the card (Klipper): steps, limit, current offset, whether "save" is on."""
        coordinator = self.coordinator
        if not coordinator.babystep_enabled:
            return None
        return {
            "z_offset": coordinator.z_offset,
            "steps": list(BABYSTEP_STEPS),
            "limit": BABYSTEP_LIMIT,
            "save": coordinator.babystep_save is not None,
        }

    def _printer_attrs(self) -> dict[str, Any]:
        """Configuration the FDM Printers panel needs to draw and edit this printer (no secrets)."""
        conf = self.coordinator.conf
        return {
            "entry_id": self.coordinator.config_entry.entry_id,
            "printer_type": str(self.coordinator.printer_type),
            "host": conf.get(CONF_HOST),
            "web_url": self.coordinator.configuration_url,
            "switch_entity": conf.get(CONF_SWITCH_ENTITY),
            "power_entity": conf.get(CONF_POWER_ENTITY),
            "energy_entity": conf.get(CONF_ENERGY_ENTITY),
            "price_entity": conf.get(CONF_PRICE_ENTITY),
            "price_per_kwh": conf.get(CONF_PRICE_FIXED),
            "camera_entity": self.coordinator.camera_entity,
            # Card extras: only what the printer has
            "lights": self.coordinator.light_entities(),
            "filaments": [slot.as_dict() for slot in self.coordinator.filament_slots],
            "babystep": self._babystep_attrs(),
            "alert": str(alert) if (alert := self.coordinator.watchdog.alert) else None,
            "firmware": self.coordinator.firmware.label if self.coordinator.firmware else None,
            "firmware_update": self.coordinator.firmware_update_recommended,
            "reachable": source.available
            if (source := self.coordinator.klipper or self.coordinator.bambu_lan)
            else None,
        }


class CounterSensor(PrintCostSensor, RestoreSensor):
    """Energy / cost counter of the printer: restores its last value so the first one after a restart is not lower."""

    async def async_added_to_hass(self) -> None:
        """Hand the last value shown before the restart to the coordinator (it keeps the counter at least there)."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        last_data = await self.async_get_last_sensor_data()
        if last_state is None or last_data is None or last_data.native_value is None:
            return
        self.coordinator.restore_counter(self.entity_description.key, last_data.native_value, last_state.last_updated)
