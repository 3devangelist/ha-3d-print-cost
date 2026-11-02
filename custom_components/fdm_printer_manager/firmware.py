"""The printer's firmware, from Moonraker's ``machine.system_info`` (``distribution``).

OpenCentauri Cosmos (the Elegoo Centauri Carbon firmware) before 26.10 cannot shut
its computer down from Moonraker (HTTP 500 "Error running shell command"), so the
integration falls back to SSH as root. Cosmos 26.10.1 answers ``{"result": "ok"}``
and goes off within seconds (tested 02/11/2026). Older versions get a recommendation
to update (a repair issue and a line on the card).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

COSMOS_ID = "cosmos"
# First Cosmos version whose Moonraker shuts the computer down cleanly (CalVer YY.MM[.N]).
COSMOS_CLEAN_SHUTDOWN: tuple[int, ...] = (26, 10)
COSMOS_RELEASES_URL = "https://github.com/OpenCentauri/cosmos/releases"

_VERSION = re.compile(r"\d+(?:\.\d+)*")


@dataclass(frozen=True, slots=True)
class Firmware:
    """What ``system_info.distribution`` says (id "cosmos", version "26.10.1", name "OpenCentauri Cosmos …")."""

    id: str
    version: str | None
    name: str | None

    @property
    def label(self) -> str:
        """For the device's software version: the name, else id and version."""
        return self.name or " ".join(part for part in (self.id, self.version) if part)

    @property
    def update_recommended(self) -> bool:
        """Cosmos older than 26.10. An unreadable version is no verdict (``False``)."""
        if self.id != COSMOS_ID:
            return False
        parsed = parse_version(self.version)
        return parsed is not None and older_than(parsed, COSMOS_CLEAN_SHUTDOWN)


def parse_version(version: Any) -> tuple[int, ...] | None:
    """Parse a version: "26.10.1" -> (26, 10, 1); "v0.0.7-beta" -> (0, 0, 7); nothing numeric -> ``None``."""
    match = _VERSION.search(str(version or ""))
    return tuple(int(part) for part in match.group().split(".")) if match else None


def older_than(version: tuple[int, ...], minimum: tuple[int, ...]) -> bool:
    """Compare part by part, missing parts count as 0 (26.10 == 26.10.0)."""
    size = max(len(version), len(minimum))
    return version + (0,) * (size - len(version)) < minimum + (0,) * (size - len(minimum))


def firmware_from_system_info(system_info: dict[str, Any] | None) -> Firmware | None:
    """The firmware, from Moonraker's ``system_info``; ``None`` when it does not say."""
    distribution = (system_info or {}).get("distribution")
    if not isinstance(distribution, dict):
        return None
    dist_id = str(distribution.get("id") or "").strip().lower()
    if not dist_id:
        return None
    version = distribution.get("version")
    name = distribution.get("name")
    return Firmware(
        id=dist_id,
        version=str(version).strip() if version not in (None, "") else None,
        name=str(name).strip() if name not in (None, "") else None,
    )
