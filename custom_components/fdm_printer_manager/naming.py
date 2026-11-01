"""A good default name for a printer.

Moonraker reports the printer computer's hostname. Many firmwares and images use
the same one on every machine ("cosmos" on the Elegoo Centauri Carbon, "mainsailos"
on MainsailOS...), which makes poor device and entity names, so these get a
readable name instead. Bambu Lab printers are named after their model, read from
the serial number (or the model code they announce on the network).
Pure logic, no Home Assistant import.
"""

from __future__ import annotations

import re

# Hostname → the printer it means. Lower case.
KNOWN_HOSTNAMES: dict[str, str] = {
    "cosmos": "Elegoo Centauri Carbon",
}
# Hostname patterns → the printer they mean. COSMOS 26.10 and later name the host
# "centauri-carbon-1" (and "centauri-carbon-2"...) instead of "cosmos".
KNOWN_HOSTNAME_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"centauri-carbon-\d+"), "Elegoo Centauri Carbon"),
)


def _known(key: str) -> str | None:
    """The printer a (lower-case) hostname means, if it is a known shared one."""
    if key in KNOWN_HOSTNAMES:
        return KNOWN_HOSTNAMES[key]
    for pattern, printer in KNOWN_HOSTNAME_PATTERNS:
        if pattern.fullmatch(key):
            return printer
    return None


# Hostnames of images, boards and printer firmwares, not of one printer.
GENERIC_HOSTNAMES: frozenset[str] = frozenset(
    {
        "mainsailos",
        "mainsail",
        "fluiddpi",
        "fluidd",
        "klipper",
        "moonraker",
        "octopi",
        "raspberrypi",
        "localhost",
        "orangepi",
        "btt-cb1",
        "debian",
        "armbian",
        "micromachine",  # Bambu Lab printers on the network
    }
)

# Bambu Lab: first three characters of the serial number → model.
BAMBU_SERIAL_MODELS: dict[str, str] = {
    "00M": "X1 Carbon",
    "00W": "X1",
    "03W": "X1E",
    "01S": "P1P",
    "01P": "P1S",
    "030": "A1 mini",
    "039": "A1",
    "094": "H2D",
}
# Bambu Lab: model code in the SSDP announcement (DevModel.bambu.com) → model.
BAMBU_CODE_MODELS: dict[str, str] = {
    "BL-P001": "X1 Carbon",
    "BL-P002": "X1",
    "C13": "X1E",
    "C11": "P1P",
    "C12": "P1S",
    "N1": "A1 mini",
    "N2S": "A1",
    "O1D": "H2D",
}


def is_generic(name: str | None) -> bool:
    """A name that says nothing about the printer (empty, a shared hostname)."""
    key = (name or "").strip().lower().removesuffix(".local")
    return not key or key in GENERIC_HOSTNAMES or _known(key) is not None


def friendly_name(name: str | None, host: str) -> str:
    """The name to show: a known hostname becomes the printer model, a generic one "Klipper <IP>"."""
    value = (name or "").strip()
    key = value.lower().removesuffix(".local")
    if (printer := _known(key)) is not None:
        return printer
    if not value or key in GENERIC_HOSTNAMES:
        return f"Klipper {host}"
    return value


def bambu_model(serial: str | None = None, model_code: str | None = None) -> str | None:
    """A1 mini, P1S, X1 Carbon... from the serial number or the announced model code."""
    if model_code and (model := BAMBU_CODE_MODELS.get(model_code.strip().upper())):
        return model
    return BAMBU_SERIAL_MODELS.get((serial or "").strip().upper()[:3])


def bambu_name(serial: str | None, model_code: str | None = None, name: str | None = None) -> str:
    """Default name of a Bambu Lab printer: "Bambu Lab A1 mini"; else a real name it announced; else "Bambu 1234"."""
    if model := bambu_model(serial, model_code):
        return f"Bambu Lab {model}"
    if name and not is_generic(name):
        return name.strip()
    return f"Bambu {(serial or '????')[-4:]}"
