"""Klipper extras found on the printer itself: lights and live Z offset ("baby steps").

What a printer has is read once from ``printer.objects.list`` and
``printer.gcode.help`` (both read-only): only lights Klipper can switch and
commands it really knows are offered, so a printer without them shows nothing.

Pure functions, unit-tested without a printer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# Klipper objects that are LEDs (SET_LED)
LED_PREFIXES = ("led", "neopixel", "dotstar", "pca9533", "pca9632")
# An ``output_pin`` is a light when its name says so ("caselight", "Luz_interior", "chamber_led", ...)
LIGHT_WORDS = ("light", "led", "lamp", "luz", "caselight", "chamber", "case")

# Baby steps: one tap moves the nozzle by one of these (mm); never more than BABYSTEP_LIMIT away from 0.
BABYSTEP_STEPS = (0.005, 0.01, 0.025, 0.05)
BABYSTEP_LIMIT = 1.0
BABYSTEP_RESET = "SET_GCODE_OFFSET Z=0 MOVE=1"
# Probes: Z_OFFSET_APPLY_PROBE saves the offset into the probe's z_offset; without a probe, the endstop's.
_PROBE_PREFIXES = (
    "probe",
    "bltouch",
    "smart_effector",
    "probe_eddy_current",
    "load_cell_probe",
    "beacon",
    "cartographer",
)


@dataclass(frozen=True, slots=True)
class KlipperLight:
    """A light Klipper can switch."""

    obj: str  # Klipper object: "led case", "output_pin Luz_interior"
    kind: str  # "led" (SET_LED) or "pin" (SET_PIN)
    name: str  # the object's own name: "case", "Luz_interior"

    @property
    def key(self) -> str:
        """Stable key for the entity's unique id: ``light_case``, ``light_luz_interior``."""
        return "light_" + re.sub(r"[^a-z0-9]+", "_", self.name.lower()).strip("_")

    @property
    def label(self) -> str:
        """Name shown to the user: "Case", "Luz interior"."""
        text = self.name.replace("_", " ").strip()
        return text[:1].upper() + text[1:] if text else "Light"


@dataclass(frozen=True, slots=True)
class KlipperFeatures:
    """What a Klipper printer offers besides printing."""

    lights: tuple[KlipperLight, ...] = ()
    babystep: bool = False  # knows SET_GCODE_OFFSET
    save_gcode: str | None = None  # Z_OFFSET_APPLY_PROBE / Z_OFFSET_APPLY_ENDSTOP
    afc_lanes: tuple[str, ...] = ()  # Box Turtle / AFC / Elegoo CANVAS lanes
    mmu: bool = False  # Happy Hare
    spoolman: bool = False  # Moonraker's spoolman component
    extra_objects: tuple[str, ...] = field(default=())

    @property
    def has_filament_system(self) -> bool:
        """A multi-material unit is fitted (AFC lanes or Happy Hare)."""
        return bool(self.afc_lanes) or self.mmu


def _is_light_pin(name: str) -> bool:
    low = name.lower()
    return any(word in low for word in LIGHT_WORDS)


def find_lights(objects: Iterable[str], commands: Iterable[str]) -> list[KlipperLight]:
    """Lights among the printer's objects, only when Klipper has the command to switch them."""
    commands = {c.upper() for c in commands}
    lights: list[KlipperLight] = []
    for obj in objects:
        prefix, _, name = obj.partition(" ")
        name = name.strip()
        if not name:
            continue
        if prefix in LED_PREFIXES and "SET_LED" in commands:
            lights.append(KlipperLight(obj, "led", name))
        elif prefix == "output_pin" and "SET_PIN" in commands and _is_light_pin(name):
            lights.append(KlipperLight(obj, "pin", name))
    return lights


def save_gcode(objects: Iterable[str], commands: Iterable[str]) -> str | None:
    """The command that keeps a baby-stepped offset (still needs SAVE_CONFIG); ``None`` if there is none."""
    commands = {c.upper() for c in commands}
    has_probe = any(o.split(" ")[0] in _PROBE_PREFIXES for o in objects)
    if has_probe and "Z_OFFSET_APPLY_PROBE" in commands:
        return "Z_OFFSET_APPLY_PROBE"
    if "Z_OFFSET_APPLY_ENDSTOP" in commands:
        return "Z_OFFSET_APPLY_ENDSTOP"
    return None


def discover(objects: Iterable[str], commands: Iterable[str], components: Iterable[str] = ()) -> KlipperFeatures:
    """Read ``objects.list``, ``gcode.help`` (command names) and ``server.info`` components."""
    objects = list(objects)
    commands = {c.upper() for c in commands}
    lights = tuple(find_lights(objects, commands))
    babystep = "SET_GCODE_OFFSET" in commands
    afc_lanes = tuple(o.partition(" ")[2] for o in objects if o.startswith("AFC_lane "))
    mmu = "mmu" in objects
    extra = [light.obj for light in lights]
    if babystep:
        extra.append("gcode_move")
    if afc_lanes:
        extra.append("AFC")
        extra.extend(f"AFC_lane {lane}" for lane in afc_lanes)
    if mmu:
        extra.append("mmu")
    return KlipperFeatures(
        lights=lights,
        babystep=babystep,
        save_gcode=save_gcode(objects, commands) if babystep else None,
        afc_lanes=afc_lanes,
        mmu=mmu,
        spoolman="spoolman" in set(components),
        extra_objects=tuple(extra),
    )


# --------------------------------------------------------------------------- #
# Lights
# --------------------------------------------------------------------------- #
def light_level(light: KlipperLight, status: dict[str, Any]) -> float | None:
    """0 = off, above 0 = on (the brightest channel for an LED). ``None`` when not read."""
    data = status.get(light.obj)
    if not isinstance(data, dict):
        return None
    if light.kind == "pin":
        value = data.get("value")
        return float(value) if isinstance(value, int | float) else None
    colors = data.get("color_data")
    if not isinstance(colors, list) or not colors:
        return None
    channels = [c for led in colors if isinstance(led, list) for c in led if isinstance(c, int | float)]
    return max(channels) if channels else 0.0


def light_color(light: KlipperLight, status: dict[str, Any]) -> tuple[float, ...] | None:
    """The LED's first colour (r, g, b, w), or the pin's value, to turn it back on as it was."""
    data = status.get(light.obj) or {}
    if light.kind == "pin":
        value = data.get("value")
        return (float(value),) if isinstance(value, int | float) else None
    colors = data.get("color_data")
    if isinstance(colors, list) and colors and isinstance(colors[0], list):
        return tuple(float(c) for c in colors[0])
    return None


def light_gcode(light: KlipperLight, on: bool, last: tuple[float, ...] | None = None) -> str:
    """SET_LED / SET_PIN to turn a light on (as it was last lit, else full) or off."""
    if light.kind == "pin":
        value = (last[0] if last and last[0] > 0 else 1.0) if on else 0.0
        return f"SET_PIN PIN={light.name} VALUE={value:g}"
    if on:
        r, g, b, w = ((*last, 0.0, 0.0, 0.0, 0.0)[:4]) if last and any(c > 0 for c in last) else (1.0, 1.0, 1.0, 1.0)
    else:
        r = g = b = w = 0.0
    return f"SET_LED LED={light.name} RED={r:g} GREEN={g:g} BLUE={b:g} WHITE={w:g}"


# --------------------------------------------------------------------------- #
# Baby steps
# --------------------------------------------------------------------------- #
def z_offset(status: dict[str, Any]) -> float | None:
    """The live Z offset (``gcode_move.homing_origin[2]``), mm."""
    origin = (status.get("gcode_move") or {}).get("homing_origin")
    if isinstance(origin, list) and len(origin) > 2 and isinstance(origin[2], int | float):
        return round(float(origin[2]), 4)
    return None


def babystep_gcode(delta: float, current: float | None) -> str:
    """``SET_GCODE_OFFSET Z_ADJUST=±delta MOVE=1``; refuses odd steps and going past ±BABYSTEP_LIMIT."""
    if not any(abs(abs(delta) - step) < 1e-9 for step in BABYSTEP_STEPS):
        raise ValueError(f"a baby step is one of {', '.join(f'{s:g}' for s in BABYSTEP_STEPS)} mm")
    if abs((current or 0.0) + delta) > BABYSTEP_LIMIT + 1e-9:
        raise ValueError(f"the Z offset stays within ±{BABYSTEP_LIMIT:g} mm")
    return f"SET_GCODE_OFFSET Z_ADJUST={delta:+.3f} MOVE=1"
