"""A good default name for a printer.

Moonraker reports the printer computer's hostname. Many firmwares and images use
the same one on every machine ("cosmos" on the Elegoo Centauri Carbon, "mainsailos"
on MainsailOS...), which makes poor device and entity names, so these get a
readable name instead. Pure logic, no Home Assistant import.
"""

from __future__ import annotations

# Hostname → the printer it means. Lower case.
KNOWN_HOSTNAMES: dict[str, str] = {
    "cosmos": "Elegoo Centauri Carbon",
}

# Hostnames of images and boards, not of a printer.
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
    }
)


def friendly_name(name: str | None, host: str) -> str:
    """The name to show: a known hostname becomes the printer model, a generic one "Klipper <IP>"."""
    value = (name or "").strip()
    key = value.lower().removesuffix(".local")
    if key in KNOWN_HOSTNAMES:
        return KNOWN_HOSTNAMES[key]
    if not value or key in GENERIC_HOSTNAMES:
        return f"Klipper {host}"
    return value
