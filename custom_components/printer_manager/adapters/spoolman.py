"""Read-only adapter for Spoolman.

Only ``GET`` requests are ever sent: this integration never changes the stock.
HaspelSync stays responsible for booking the consumption.

Endpoints used (Spoolman REST API v1):

* ``GET /api/v1/info``                 -> connectivity check
* ``GET /api/v1/spool/{id}``           -> spool, with nested filament & vendor
* ``GET /api/v1/setting/currency``     -> configured currency (optional)
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

import aiohttp

from ..models import SpoolInfo
from ..units import to_decimal

_LOGGER = logging.getLogger(__name__)

# Spoolman "extra" custom fields recognised as explicit unit prices.
_EXTRA_PER_KG = {"price_per_kg", "price_kg", "prix_kg", "prix_par_kg", "cost_per_kg"}
_EXTRA_PER_G = {"price_per_g", "price_per_gram", "prix_g", "prix_par_g", "cost_per_g"}


class SpoolmanError(Exception):
    """Spoolman unreachable or answered something unusable."""


class SpoolNotFoundError(SpoolmanError):
    """The spool id does not exist (anymore)."""


# --------------------------------------------------------------------------- #
# Pure parsing
# --------------------------------------------------------------------------- #
def _extra_value(extra: Any, names: set[str]) -> Decimal | None:
    """Spoolman stores extra fields as JSON-encoded strings."""
    if not isinstance(extra, dict):
        return None
    for key, raw in extra.items():
        if key.lower() not in names:
            continue
        value: Any = raw
        if isinstance(raw, str):
            try:
                value = json.loads(raw)
            except ValueError:
                value = raw
        dec = to_decimal(value)
        if dec is not None:
            return dec
    return None


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_spool(payload: Any) -> SpoolInfo:
    """Parse ``GET /api/v1/spool/{id}``."""
    if not isinstance(payload, dict) or "id" not in payload:
        raise SpoolmanError("spool payload is not an object")
    filament = payload.get("filament") or {}
    vendor = filament.get("vendor") or {}
    color = filament.get("color_hex") or (filament.get("multi_color_hexes") or "").split(",")[0] or None
    extras: dict[str, Any] = {**(filament.get("extra") or {}), **(payload.get("extra") or {})}
    return SpoolInfo(
        spool_id=int(payload["id"]),
        filament_id=filament.get("id"),
        vendor=vendor.get("name") if isinstance(vendor, dict) else None,
        name=filament.get("name"),
        material=filament.get("material"),
        color_hex=f"#{color.lstrip('#').upper()}" if color else None,
        spool_price=to_decimal(payload.get("price")),
        filament_price=to_decimal(filament.get("price")),
        initial_weight_g=_float(payload.get("initial_weight")),
        filament_weight_g=_float(filament.get("weight")),
        remaining_weight_g=_float(payload.get("remaining_weight")),
        price_per_kg_extra=_extra_value(extras, _EXTRA_PER_KG),
        price_per_g_extra=_extra_value(extras, _EXTRA_PER_G),
        archived=bool(payload.get("archived", False)),
    )


def parse_setting_value(payload: Any) -> str | None:
    """Parse ``GET /api/v1/setting/{key}`` (value is JSON-encoded)."""
    if not isinstance(payload, dict):
        return None
    raw = payload.get("value")
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except ValueError:
            return raw or None
        return str(decoded) if decoded else None
    return None


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #
class SpoolmanClient:
    """Async, read-only Spoolman client."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str, *, timeout: float = 10.0) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    @property
    def base_url(self) -> str:
        """Base URL."""
        return self._base_url

    async def _get(self, path: str) -> Any:
        url = f"{self._base_url}{path}"
        try:
            async with self._session.get(url, timeout=self._timeout) as resp:
                if resp.status == 404:
                    raise SpoolNotFoundError(path)
                if resp.status != 200:
                    raise SpoolmanError(f"{path}: HTTP {resp.status}")
                return await resp.json(content_type=None)
        except SpoolmanError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise SpoolmanError(f"{path}: {type(err).__name__}") from err

    async def get_info(self) -> dict[str, Any]:
        """Server info, used to validate the URL."""
        data = await self._get("/api/v1/info")
        if not isinstance(data, dict):
            raise SpoolmanError("info is not an object")
        return data

    async def get_spool(self, spool_id: int) -> SpoolInfo:
        """One spool with its filament and vendor."""
        return parse_spool(await self._get(f"/api/v1/spool/{int(spool_id)}"))

    async def get_currency(self) -> str | None:
        """Currency configured in Spoolman, if any."""
        try:
            return parse_setting_value(await self._get("/api/v1/setting/currency"))
        except SpoolmanError:
            return None
