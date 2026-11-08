"""Filament materials: one name per material, and a generic price per kg for each (0.15.0).

Pure logic, no Home Assistant import.

The material of a print is read, never guessed: the slicer's ``filament_type``
(Moonraker metadata), the spool loaded while printing (AFC / CANVAS lane, Happy
Hare gate, Spoolman's active spool, Bambu AMS tray), or a material written in the
file name ("…_ASA_….gcode"). Names are made the same ("Generic PETG", "PETG HF",
"PETG+" → ``PETG``). When nothing says it, or two different materials are named,
the material stays unknown (``None``), shown as "?" and chosen by hand.
"""

from __future__ import annotations

import re
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


def set_row_material(
    row: dict[str, Any],
    material: str,
    printer_price: Decimal | None,
    table: dict[str, Decimal],
    currency: str,
) -> None:
    """A print of the history gets its material by hand; its filament cost is worked out again.

    A price set on the printer when the print ended is kept (it does not depend on
    the material); otherwise the generic price of the new material is used.
    """
    row["filament_type"] = material
    row["filament_type_source"] = SOURCE_USER
    if row.get("filament_price_source") == PRICE_PRINTER and _price(row.get("filament_price_per_kg")) is not None:
        printer_price = _price(row.get("filament_price_per_kg"))
    price, source = resolve_price(printer_price, material, table)
    row["filament_price_per_kg"] = float(price) if price is not None else None
    row["filament_price_source"] = source
    grams = _price(row.get("filament_g"))
    row["filament_cost"] = (
        float(round(grams * price / Decimal(1000), 4)) if grams is not None and price is not None else None
    )
    notes = [str(n) for n in row.get("notes") or [] if not is_price_note(str(n))]
    if grams is not None and (note := price_note(material, price, source, currency)):
        notes.append(note)
    row["notes"] = notes
    row["print_cost"] = total_cost(row.get("electricity_cost"), row.get("filament_cost"))


def total_cost(electricity: Any, filament: Any) -> float | None:
    """Electricity + filament; only the known part when one is unknown; ``None`` when both are."""
    parts = [p for p in (_price(electricity), _price(filament)) if p is not None]
    return float(round(sum(parts, Decimal(0)), 4)) if parts else None
