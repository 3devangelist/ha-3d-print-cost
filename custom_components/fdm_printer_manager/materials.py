"""Filament materials: one name per material, and a generic price per kg for each (0.15.0).

Pure logic, no Home Assistant import.

The material of a print is read, never guessed: the slicer's ``filament_type``
(Moonraker metadata), the spool loaded while printing (AFC / CANVAS lane, Happy
Hare gate, Spoolman's active spool, Bambu AMS tray), or a material written in the
file name ("…_ASA_….gcode"). Names are made the same ("Generic PETG", "PETG HF",
"PETG+" → ``PETG``). When nothing says it, or two different materials are named,
the material stays unknown (``None``), shown as "?" and chosen by hand.

0.15.2: the grams of a print come from the length really used. The slicer's
weight of the file is used when it was written for the same material; otherwise
the length is weighed with the material's density (``DENSITIES``).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

# The materials offered in the panel's list, in this order.
MATERIALS: tuple[str, ...] = (
    "PLA",
    "PLA-CF",
    "PETG",
    "PETG-CF",
    "PET",
    "PET-CF",
    "ABS",
    "ASA",
    "TPU",
    "PC",
    "PA",
    "PP",
    "PPA",
    "PPS",
)

# What the panel shows next to the short name.
LABELS: dict[str, str] = {"PA": "PA (Nylon)"}

# Generic prices per kg (EUR, November 2026: usual 1 kg spools of known brands in Europe, rounded).
# Only a first idea of the cost: each one can be changed in the options of "FDM Printers", and a price
# set on a printer always wins.
DEFAULT_PRICES: dict[str, Decimal] = {
    "PLA": Decimal(20),
    "PLA-CF": Decimal(35),
    "PETG": Decimal(20),
    "PETG-CF": Decimal(35),
    "PET": Decimal(25),
    "PET-CF": Decimal(50),
    "ABS": Decimal(20),
    "ASA": Decimal(25),
    "TPU": Decimal(30),
    "PC": Decimal(35),
    "PA": Decimal(40),
    "PP": Decimal(35),
    "PPA": Decimal(60),
    "PPS": Decimal(80),
}

# Typical density of each material (g/cm³, usual datasheet values of known brands, 0.15.2). Used to weigh
# the filament from the length really used when the slicer did not write the weight, or wrote it for
# another material than the one chosen by hand. Variants (PA12, PPS-CF…) share their base material's value.
DENSITIES: dict[str, float] = {
    "PLA": 1.24,
    "PLA-CF": 1.29,
    "PETG": 1.27,
    "PETG-CF": 1.30,
    "PET": 1.38,
    "PET-CF": 1.30,
    "ABS": 1.04,
    "ASA": 1.07,
    "TPU": 1.21,
    "PC": 1.20,
    "PA": 1.14,
    "PP": 0.90,
    "PPA": 1.20,
    "PPS": 1.35,
}
# Filament diameter when the slicer does not say it (mm).
DEFAULT_DIAMETER_MM = 1.75
# Values from a slicer are only trusted inside these bounds.
_DIAMETER_RANGE = (1.0, 3.5)
_DENSITY_RANGE = (0.5, 3.0)

# Where the grams of a print came from.
GRAMS_SLICER = "slicer"  # the slicer's weight of the file, per mm, times the length used
GRAMS_DENSITY = "density"  # length used x section of the filament x density

# Where the material of a print came from.
SOURCE_SLICER = "slicer"
SOURCE_SPOOL = "spool"
SOURCE_FILE_NAME = "file_name"
SOURCE_USER = "user"

# Where the price per kg of a print came from.
PRICE_PRINTER = "printer"
PRICE_GENERIC = "generic"

# One word of a name → material. Variants (PA6, PA12, PAHT, NYLON, PLA+, PP-GF…) are the base material;
# a carbon-fibre suffix ("CF") makes PLA, PETG and PET their own -CF material.
_WORDS: dict[str, str] = {
    "PLA": "PLA",
    "PLACF": "PLA-CF",
    "PETG": "PETG",
    "PETGCF": "PETG-CF",
    "PET": "PET",
    "PETCF": "PET-CF",
    "ABS": "ABS",
    "ASA": "ASA",
    "TPU": "TPU",
    "FLEX": "TPU",
    "PC": "PC",
    "PA": "PA",
    "NYLON": "PA",
    "PAHT": "PA",
    "PACF": "PA",
    "PAHTCF": "PA",
    "PP": "PP",
    "PPCF": "PP",
    "PPGF": "PP",
    "PPA": "PPA",
    "PPACF": "PPA",
    "PPAGF": "PPA",
    "PPS": "PPS",
    "PPSCF": "PPS",
}
_PA_NUMBER = re.compile(r"^PA\d{1,2}(CF|GF)?$")  # PA6, PA12, PA66, PA6CF
_TPU_HARDNESS = re.compile(r"^TPU\d{2}[AD]?$")  # TPU95A
_CF_BASES = {"PLA": "PLA-CF", "PETG": "PETG-CF", "PET": "PET-CF"}


def _word(word: str) -> str | None:
    if word in _WORDS:
        return _WORDS[word]
    if _PA_NUMBER.match(word):
        return "PA"
    if _TPU_HARDNESS.match(word):
        return "TPU"
    return None


def _materials_in(text: str) -> set[str]:
    words = re.findall(r"[A-Z0-9]+", text.upper())
    found: set[str] = set()
    skip = False
    for index, word in enumerate(words):
        if skip:
            skip = False
            continue
        following = words[index + 1] if index + 1 < len(words) else ""
        if word == "PET" and following == "G":  # "PET-G"
            found.add("PETG")
            skip = True
            continue
        material = _word(word)
        if material is None:
            continue
        if following in ("CF", "GF"):
            material = _CF_BASES.get(material, material) if following == "CF" else material
            skip = True
        found.add(material)
    return found


def normalize_material(text: Any) -> str | None:
    """The material named by a slicer or a spool ("Generic PETG", "PLA+", "PA6-CF"), ``None`` when unclear.

    Several filaments ("ASA;ASA") give the material when they all name the same one.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    found: set[str] = set()
    for part in re.split(r"[;,|]", raw):
        if not part.strip():
            continue
        names = _materials_in(part)
        if len(names) != 1:
            return None
        found |= names
    return found.pop() if len(found) == 1 else None


def material_from_file_name(name: Any) -> str | None:
    """A material written in the file name ("Vespa_ASA_14h37m.gcode" → ASA). ``None`` when none or several."""
    stem = re.sub(r"\.(gcode|g|gco|3mf|bgcode)$", "", str(name or "").strip(), flags=re.IGNORECASE)
    names = _materials_in(stem)
    return names.pop() if len(names) == 1 else None


def price_key(material: str) -> str:
    """Option key of a material's price: ``PETG-CF`` → ``price_petg_cf``."""
    return "price_" + material.lower().replace("-", "_")


def _price(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() and number >= 0 else None


def generic_prices(options: Any) -> dict[str, Decimal]:
    """The table of prices per kg: the defaults, changed by the options of "FDM Printers" (``price_<material>``).

    A price cleared in the options means "no generic price" for that material.
    """
    table = dict(DEFAULT_PRICES)
    if not isinstance(options, dict):
        return table
    for material in MATERIALS:
        key = price_key(material)
        if key not in options:
            continue
        if options[key] in (None, ""):
            table.pop(material, None)
        elif (price := _price(options[key])) is not None:
            table[material] = price  # an unreadable value keeps the default
    return table


def resolve_price(
    printer_price: Decimal | None, material: str | None, table: dict[str, Decimal]
) -> tuple[Decimal | None, str | None]:
    """Price per kg of a print and where it came from: the printer's own price, else the material's generic price."""
    if printer_price is not None:
        return printer_price, PRICE_PRINTER
    if material and material in table:
        return table[material], PRICE_GENERIC
    return None, None


def price_note(material: str | None, price: Decimal | None, source: str | None, currency: str) -> str | None:
    """The note of a print about its filament price (generic price used, or no price)."""
    if source == PRICE_GENERIC and price is not None:
        return f"Filament cost at the generic price for {material} ({price.normalize():f} {currency}/kg)"
    if source is None:
        if not material:
            return "Filament cost unknown: material unknown (choose it in Recent prints)"
        return f"Filament cost unknown: no price for {material}"
    return None


# Notes this module writes: replaced when the material of a print is changed.
_PRICE_NOTE_STARTS = ("Filament cost at the generic price", "Filament cost unknown: material unknown")
_PRICE_NOTE_NO_PRICE = "Filament cost unknown: no price for "


def is_price_note(note: str) -> bool:
    """A note written by :func:`price_note`."""
    return note.startswith(_PRICE_NOTE_STARTS) or note.startswith(_PRICE_NOTE_NO_PRICE)


# --------------------------------------------------------------------------- #
# Grams from the length used (0.15.2)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Weight:
    """Grams of filament a print used, and how they were worked out."""

    grams: float | None
    source: str | None = None  # GRAMS_SLICER, GRAMS_DENSITY, None when unknown
    density: float | None = None  # g/cm³ used (density source only)
    diameter: float | None = None  # mm used (density source only)
    slicer_density: bool = False  # the density is the slicer's, not the material's typical one


def _in_range(value: Any, bounds: tuple[float, float]) -> float | None:
    number = _number(value)
    return number if number is not None and bounds[0] <= number <= bounds[1] else None


def _number(value: Any) -> float | None:
    try:
        number = None if value is None or isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        return None
    return number if number is not None and math.isfinite(number) else None


def _length(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number >= 0 else None


def grams_from_length(length_mm: float, density: float, diameter_mm: float = DEFAULT_DIAMETER_MM) -> float:
    """Weight of a length of filament: length x section of the round filament x density (mm³ → cm³)."""
    section_mm2 = math.pi * (diameter_mm / 2) ** 2
    return length_mm * section_mm2 * density / 1000


def filament_weight(
    length_mm: Any,
    material: str | None,
    *,
    g_per_mm: Any = None,
    file_material: str | None = None,
    density: Any = None,
    diameter: Any = None,
) -> Weight:
    """Grams a print used, from the length really used and the material.

    The slicer's weight of the file (grams per mm) is used when it was written for
    this material (or either material is unknown). Otherwise the length is weighed
    with a density: the slicer's (same rule), else the material's typical density
    (:data:`DENSITIES`), on a filament of the slicer's diameter, else 1.75 mm.
    Unknown (``None``) when the length is unknown, or no density applies.
    """
    length = _length(length_mm)
    if length is None:
        return Weight(None)
    same = file_material is None or material is None or file_material == material
    per_mm = _number(g_per_mm)
    if same and per_mm is not None and per_mm > 0:
        return Weight(length * per_mm, GRAMS_SLICER)
    slicer_density = _in_range(density, _DENSITY_RANGE) if same else None
    rho = slicer_density if slicer_density is not None else DENSITIES.get(material or "")
    if rho is None:
        return Weight(None)
    dia = _in_range(diameter, _DIAMETER_RANGE) or DEFAULT_DIAMETER_MM
    return Weight(grams_from_length(length, rho, dia), GRAMS_DENSITY, rho, dia, slicer_density is not None)


_GRAMS_NOTE = "Filament weight "


def grams_note(material: str | None, weight: Weight) -> str | None:
    """The note of a print about where its grams came from (or why they are unknown)."""
    if weight.source == GRAMS_SLICER:
        return "Filament weight from the slicer"
    if weight.source == GRAMS_DENSITY:
        whose = "the slicer's density" if weight.slicer_density else f"the typical density of {material}"
        return (
            f"Filament weight from the length used and {whose} "
            f"({weight.density:g} g/cm³, {weight.diameter:g} mm filament)"
        )
    if not material:
        return "Filament weight unknown: material unknown (choose it in Recent prints)"
    return f"Filament weight unknown: no slicer weight and no density for {material}"


def is_grams_note(note: str) -> bool:
    """A note written by :func:`grams_note` (or by 0.14.0 when the slicer gave no weight)."""
    return note.startswith(_GRAMS_NOTE)


def _upgrade_row(row: dict[str, Any]) -> None:
    """A print saved before 0.15.2: its grams were the slicer's; keep what is needed to weigh it again."""
    if row.get("filament_g_source") or _length(row.get("filament_g")) is None:
        return
    row["filament_g_source"] = GRAMS_SLICER
    length = _length(row.get("filament_mm"))
    if row.get("filament_g_per_mm") is None and length:
        row["filament_g_per_mm"] = float(row["filament_g"]) / length
    if "filament_file_type" not in row:
        slicer = row.get("filament_type_source") == SOURCE_SLICER
        row["filament_file_type"] = normalize_material(row.get("filament_type")) if slicer else None


def refresh_row_filament(
    row: dict[str, Any],
    printer_price: Decimal | None,
    table: dict[str, Decimal],
    currency: str,
) -> None:
    """Grams, price per kg, cost and notes of a print of the history, from its length and material.

    A price set on the printer when the print ended is kept (it does not depend on
    the material); otherwise the printer's price now, else the generic price of the material.
    """
    _upgrade_row(row)
    material = row.get("filament_type") or None
    length = _length(row.get("filament_mm"))
    if length is None:
        # No length: nothing to weigh. Grams saved before 0.15.2 without a length stay as they were.
        weight = Weight(_length(row.get("filament_g")), row.get("filament_g_source"))
    else:
        weight = filament_weight(
            length,
            material,
            g_per_mm=row.get("filament_g_per_mm"),
            file_material=row.get("filament_file_type"),
            density=row.get("filament_density"),
            diameter=row.get("filament_diameter_mm"),
        )
        row["filament_m"] = round(length / 1000, 2)
    row["filament_g"] = round(weight.grams, 2) if weight.grams is not None else None
    row["filament_g_source"] = weight.source
    if row.get("filament_price_source") == PRICE_PRINTER and _price(row.get("filament_price_per_kg")) is not None:
        printer_price = _price(row.get("filament_price_per_kg"))
    price, source = resolve_price(printer_price, material, table)
    row["filament_price_per_kg"] = float(price) if price is not None else None
    row["filament_price_source"] = source
    grams = _price(row.get("filament_g"))
    row["filament_cost"] = (
        float(round(grams * price / Decimal(1000), 4)) if grams is not None and price is not None else None
    )
    notes = [
        str(n)
        for n in row.get("notes") or []
        if not is_price_note(str(n))
        and not (length is not None and is_grams_note(str(n)))
        and not (length is not None and str(n).startswith("Filament used unknown"))
    ]
    if length is not None and (note := grams_note(material, weight)):
        notes.append(note)
    if grams is not None and (note := price_note(material, price, source, currency)):
        notes.append(note)
    row["notes"] = notes
    row["print_cost"] = total_cost(row.get("electricity_cost"), row.get("filament_cost"))


def set_row_length(
    row: dict[str, Any],
    length_mm: float,
    *,
    g_per_mm: float | None = None,
    file_type: str | None = None,
    density: float | None = None,
    diameter_mm: float | None = None,
) -> None:
    """An old print gets the length it used (read later from the printer's history).

    Also what the slicer wrote for its file. Call :func:`refresh_row_filament` afterwards
    for the grams and the cost.
    """
    row["filament_mm"] = round(length_mm, 1)
    row["filament_m"] = round(length_mm / 1000, 2)
    if (per_mm := _number(g_per_mm)) is not None and per_mm > 0:
        row["filament_g_per_mm"] = per_mm
    if (value := _in_range(density, _DENSITY_RANGE)) is not None:
        row["filament_density"] = value
    if (value := _in_range(diameter_mm, _DIAMETER_RANGE)) is not None:
        row["filament_diameter_mm"] = value
    if file_type and str(file_type).strip():
        row["filament_file_type"] = normalize_material(file_type)
        if not row.get("filament_type"):
            row["filament_type"] = row["filament_file_type"] or str(file_type).strip()
            row["filament_type_source"] = SOURCE_SLICER


def set_row_material(
    row: dict[str, Any],
    material: str,
    printer_price: Decimal | None,
    table: dict[str, Decimal],
    currency: str,
) -> None:
    """A print of the history gets its material by hand; its grams and cost are worked out again.

    The slicer's weight stays when it was for this material; otherwise the length is weighed with
    the density of the new material.
    """
    _upgrade_row(row)  # before the material changes: the slicer's material is still known
    row["filament_type"] = material
    row["filament_type_source"] = SOURCE_USER
    refresh_row_filament(row, printer_price, table, currency)


def total_cost(electricity: Any, filament: Any) -> float | None:
    """Electricity + filament; only the known part when one is unknown; ``None`` when both are."""
    parts = [p for p in (_price(electricity), _price(filament)) if p is not None]
    return float(round(sum(parts, Decimal(0)), 4)) if parts else None
