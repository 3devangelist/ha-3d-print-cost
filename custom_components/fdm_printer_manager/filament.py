"""Filaments loaded in a multi-material unit, for the card and a sensor.

Read only, from what the printer already reports:

* Klipper with AFC (Box Turtle, Elegoo CANVAS on the Centauri Carbon "cosmos"
  firmware): ``AFC`` and ``AFC_lane <name>``: material, colour, weight;
* Klipper with Happy Hare: ``mmu``: ``gate_material``, ``gate_color``, ``gate_status``;
* Klipper with Spoolman (no unit): the active spool;
* Bambu Lab over LAN: the AMS trays (``print.ams``) and the external spool (``vt_tray``).

Nothing is returned for a printer without one, so the card shows nothing.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

_HEX = re.compile(r"^#?([0-9a-fA-F]{6})([0-9a-fA-F]{2})?$")
BAMBU_NO_TRAY = "255"
BAMBU_EXTERNAL = "254"


@dataclass(frozen=True, slots=True)
class FilamentSlot:
    """One slot (lane, gate, tray) of a filament unit."""

    index: int  # position in the unit, from 0
    name: str  # "CANVAS_1", "Gate 0", "AMS 1 · 2", "External"
    material: str | None = None  # "PLA", "PETG"
    color: str | None = None  # "#RRGGBB"
    remaining: float | None = None  # percent left, when the unit knows
    active: bool = False  # feeding the nozzle now
    empty: bool = False
    tool: str | None = None  # "T0"

    def as_dict(self) -> dict[str, Any]:
        """For state attributes and the panel."""
        return asdict(self)


def _num(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def norm_color(value: Any) -> str | None:
    """Colour as ``#RRGGBB`` (also from Bambu's RGBA ``FFFFFFFF``); a colour name is kept."""
    text = str(value or "").strip()
    if not text:
        return None
    if match := _HEX.match(text):
        return f"#{match.group(1).upper()}"
    return text.lower() if text.isalpha() else None


def _pct(left: Any, total: Any) -> float | None:
    left, total = _num(left), _num(total)
    if left is None or not total or total <= 0:
        return None
    return round(max(0.0, min(100.0, left / total * 100)), 1)


# --------------------------------------------------------------------------- #
# Klipper
# --------------------------------------------------------------------------- #
def afc_slots(status: dict[str, Any]) -> list[FilamentSlot]:
    """AFC lanes (Box Turtle, Elegoo CANVAS)."""
    afc = status.get("AFC")
    if not isinstance(afc, dict):
        return []
    current = afc.get("current_lane") or afc.get("current_load")
    slots: list[FilamentSlot] = []
    for index, lane in enumerate(afc.get("lanes") or []):
        data = status.get(f"AFC_lane {lane}")
        if not isinstance(data, dict):
            continue
        material = str(data.get("material") or "").strip() or None
        weight = _num(data.get("weight"))
        slots.append(
            FilamentSlot(
                index=index,
                name=str(lane),
                material=material,
                color=norm_color(data.get("color")),
                remaining=_pct(weight, data.get("initial_weight")) if material else None,
                active=lane == current or bool(data.get("tool_loaded")),
                empty=not material and not weight and not data.get("load") and not data.get("prep"),
                tool=data.get("map") or None,
            )
        )
    return slots


def mmu_slots(status: dict[str, Any]) -> list[FilamentSlot]:
    """Happy Hare gates. ``gate_status``: -1 unknown, 0 empty, 1 available, 2 from buffer."""
    mmu = status.get("mmu")
    if not isinstance(mmu, dict):
        return []
    materials = mmu.get("gate_material") or []
    colors = mmu.get("gate_color") or []
    states = mmu.get("gate_status") or []
    current = mmu.get("gate")
    count = max(len(materials), len(colors), len(states))
    slots: list[FilamentSlot] = []
    for gate in range(count):
        material = str(materials[gate] if gate < len(materials) else "").strip() or None
        state = states[gate] if gate < len(states) else -1
        slots.append(
            FilamentSlot(
                index=gate,
                name=f"Gate {gate}",
                material=material,
                color=norm_color(colors[gate] if gate < len(colors) else None),
                active=current == gate,
                empty=state == 0,
                tool=f"T{gate}",
            )
        )
    return slots


def spoolman_slot(spool: dict[str, Any] | None) -> list[FilamentSlot]:
    """Spoolman's active spool (``/v1/spool/<id>``) as a single slot."""
    if not isinstance(spool, dict) or spool.get("id") is None:
        return []
    filament = spool.get("filament") or {}
    total = _num(spool.get("initial_weight")) or _num(filament.get("weight"))
    return [
        FilamentSlot(
            index=0,
            name=str(filament.get("name") or f"Spool {spool['id']}"),
            material=str(filament.get("material") or "").strip() or None,
            color=norm_color(filament.get("color_hex")),
            remaining=_pct(spool.get("remaining_weight"), total),
            active=True,
        )
    ]


def klipper_slots(status: dict[str, Any], spool: dict[str, Any] | None = None) -> list[FilamentSlot]:
    """The unit's slots; with no unit, Spoolman's active spool."""
    return afc_slots(status) or mmu_slots(status) or spoolman_slot(spool)


# --------------------------------------------------------------------------- #
# Bambu Lab (LAN)
# --------------------------------------------------------------------------- #
def _tray(tray: dict[str, Any], index: int, name: str, active: bool) -> FilamentSlot:
    material = str(tray.get("tray_type") or "").strip() or None
    remain = _num(tray.get("remain"))
    return FilamentSlot(
        index=index,
        name=name,
        material=material,
        color=norm_color(tray.get("tray_color")) if material else None,
        remaining=remain if material and remain is not None and remain >= 0 else None,
        active=active,
        empty=material is None,
    )


def bambu_slots(report: dict[str, Any]) -> list[FilamentSlot]:
    """AMS trays and the external spool; nothing when no AMS is fitted."""
    ams = report.get("ams")
    if not isinstance(ams, dict):
        return []
    units = [u for u in ams.get("ams") or [] if isinstance(u, dict)]
    if not units:
        return []
    now = str(ams.get("tray_now") or BAMBU_NO_TRAY)
    slots: list[FilamentSlot] = []
    for unit in units:
        unit_id = int(_num(unit.get("id")) or 0)
        for tray in unit.get("tray") or []:
            if not isinstance(tray, dict):
                continue
            tray_id = int(_num(tray.get("id")) or 0)
            index = unit_id * 4 + tray_id
            slots.append(_tray(tray, index, f"AMS {unit_id + 1} · {tray_id + 1}", now == str(index)))
    external = report.get("vt_tray")
    if isinstance(external, dict) and str(external.get("tray_type") or "").strip():
        slots.append(_tray(external, int(BAMBU_EXTERNAL), "External", now == BAMBU_EXTERNAL))
    return slots


def summary(slots: list[FilamentSlot]) -> str | None:
    """Sensor state: the active slot's material, else how many slots are loaded."""
    if not slots:
        return None
    for slot in slots:
        if slot.active and slot.material:
            return slot.material
    loaded = sum(1 for slot in slots if slot.material)
    return f"{loaded}/{len(slots)} loaded"
