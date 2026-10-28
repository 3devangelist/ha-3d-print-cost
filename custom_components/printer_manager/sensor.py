"""Sensors. Pure presentation: values come from the PrintJob model."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfMass, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_COVER_IMAGE_ENTITY, DOMAIN
from .coordinator import PrintCostConfigEntry, PrintCostCoordinator, PrintCostData
from .entity import PrintCostEntity
from .models import PrintJob, PrintResult

PARALLEL_UPDATES = 0

CURRENCY = "__currency__"  # replaced by the configured currency
CURRENCY_PER_HOUR = "__currency__/h"
CURRENCY_PER_KWH = "__currency__/kWh"


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
        "electricity_cost": summary["electricity_cost"],
        "electricity_cost_complete": summary["electricity_cost_complete"],
        "filament_status": summary["filament_status"],
        "filament_cost": summary["filament_cost"],
        "energy_kwh": summary["energy_kwh"],
        "filament_weight_g": summary["filament_weight_g"],
        "planned_weight_g": job.planned_weight_g,
        "partial_total_cost": summary["partial_total_cost"],
        "filaments": summary["filaments"],
        "notes": summary["notes"],
    }


def _last_attrs(data: PrintCostData) -> dict[str, Any] | None:
    job = data.last_job
    if job is None:
        return None
    summary = job.summary()
    return {k: v for k, v in summary.items() if k != "job_id"}


SENSORS: tuple[PrintCostSensorDescription, ...] = (
    # ---------------- current print ----------------
    PrintCostSensorDescription(
        key="current_print_total_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        value_fn=lambda d: _money(j.total_cost) if (j := _current(d)) else None,
        attrs_fn=_current_attrs,
    ),
    PrintCostSensorDescription(
        key="current_print_electricity_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=3,
        value_fn=lambda d: _money(j.electricity_cost) if (j := _current(d)) else None,
    ),
    PrintCostSensorDescription(
        key="current_print_filament_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        value_fn=lambda d: _money(j.filament_cost) if (j := _current(d)) else None,
    ),
    PrintCostSensorDescription(
        key="current_print_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        value_fn=lambda d: round(j.energy_kwh, 6) if (j := _current(d)) else None,
    ),
    PrintCostSensorDescription(
        key="current_print_filament_weight",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=1,
        value_fn=lambda d: j.filament_weight_g if (j := _current(d)) else None,
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
        key="last_print_total_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        value_fn=lambda d: _money(d.last_job.total_cost) if d.last_job else None,
        attrs_fn=_last_attrs,
    ),
    PrintCostSensorDescription(
        key="last_print_electricity_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=3,
        value_fn=lambda d: _money(d.last_job.electricity_cost) if d.last_job else None,
    ),
    PrintCostSensorDescription(
        key="last_print_filament_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        value_fn=lambda d: _money(d.last_job.filament_cost) if d.last_job else None,
    ),
    PrintCostSensorDescription(
        key="last_print_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        value_fn=lambda d: round(d.last_job.energy_kwh, 6) if d.last_job else None,
    ),
    PrintCostSensorDescription(
        key="last_print_filament_weight",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=1,
        value_fn=lambda d: d.last_job.filament_weight_g if d.last_job else None,
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
    """Create the sensors."""
    coordinator = entry.runtime_data
    async_add_entities(PrintCostSensor(coordinator, description) for description in SENSORS)


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
        """Print thumbnail on the totals, shown by cards instead of the icon.

        Current print: the ha-bambulab cover image. Last print: the copy kept
        by ``image.<printer>_last_print_image``. The URL carries an access
        token that HA rotates; the periodic update refreshes it.
        """
        data = self.coordinator.data
        if data is None:
            return None
        key = self.entity_description.key
        image: str | None = None
        if key == "current_print_total_cost" and data.current_job is not None:
            image = self.coordinator.conf.get(CONF_COVER_IMAGE_ENTITY)
        elif key == "last_print_total_cost" and self.coordinator.last_image is not None:
            image = er.async_get(self.hass).async_get_entity_id(
                "image", DOMAIN, f"{self.coordinator.config_entry.entry_id}_last_print_image"
            )
        state = self.hass.states.get(image) if image else None
        return state.attributes.get("entity_picture") if state else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Compact attributes (no large payloads)."""
        if self.coordinator.data is None or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)
