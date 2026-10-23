"""Config flow (UI only, no YAML) and options flow."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .adapters import bambu
from .adapters.electricity import validate_units
from .adapters.haspelsync import (
    HaspelsyncAuthError,
    HaspelsyncClient,
    HaspelsyncError,
    HaspelsyncHostRefusedError,
    HaspelsyncPrinter,
)
from .adapters.spoolman import SpoolmanClient, SpoolmanError
from .const import (
    BAMBU_DOMAIN,
    CONF_COVER_IMAGE_ENTITY,
    CONF_CURRENCY,
    CONF_ENERGY_ENTITY,
    CONF_FILAMENT_FALLBACK,
    CONF_GCODE_FOLDER,
    CONF_HASPELSYNC_API_KEY,
    CONF_HASPELSYNC_PRINTER_ID,
    CONF_HASPELSYNC_URL,
    CONF_HISTORY_SIZE,
    CONF_INTEGRATION_METHOD,
    CONF_JOB_NAME_ENTITY,
    CONF_POWER_ENTITY,
    CONF_PRICE_ENTITY,
    CONF_PRINT_STATUS_ENTITY,
    CONF_PRINT_WEIGHT_ENTITY,
    CONF_PRINTER_DEVICE_ID,
    CONF_PRINTER_NAME,
    CONF_PRINTER_SERIAL,
    CONF_PROGRESS_ENTITY,
    CONF_REMAINING_TIME_ENTITY,
    CONF_SPOOLMAN_URL,
    CONF_UPDATE_INTERVAL,
    DEFAULT_FILAMENT_FALLBACK,
    DEFAULT_GCODE_FOLDER,
    DEFAULT_HISTORY_SIZE,
    DEFAULT_INTEGRATION_METHOD,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    HTTP_TIMEOUT,
    MAX_HISTORY_SIZE,
    MAX_UPDATE_INTERVAL,
    MIN_HISTORY_SIZE,
    MIN_UPDATE_INTERVAL,
    NAME,
    FilamentFallback,
    IntegrationMethod,
)

_LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Schema builders (shared by config and options flows)
# --------------------------------------------------------------------------- #
def _entity(domain: str | list[str], device_class: str | None = None) -> selector.EntitySelector:
    config: dict[str, Any] = {"domain": domain}
    if device_class:
        config["device_class"] = device_class
    return selector.EntitySelector(selector.EntitySelectorConfig(**config))


def _opt(key: str, defaults: dict[str, Any]) -> vol.Optional:
    """Optional field pre-filled with a suggestion (clearable)."""
    value = defaults.get(key)
    return vol.Optional(key, description={"suggested_value": value} if value else None)


def bambu_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Bambu entities step."""
    return vol.Schema(
        {
            vol.Required(
                CONF_PRINT_STATUS_ENTITY,
                description={"suggested_value": defaults.get(CONF_PRINT_STATUS_ENTITY)},
            ): _entity("sensor"),
            _opt(CONF_JOB_NAME_ENTITY, defaults): _entity("sensor"),
            _opt(CONF_PROGRESS_ENTITY, defaults): _entity("sensor"),
            _opt(CONF_PRINT_WEIGHT_ENTITY, defaults): _entity("sensor"),
            _opt(CONF_REMAINING_TIME_ENTITY, defaults): _entity("sensor"),
            _opt(CONF_COVER_IMAGE_ENTITY, defaults): _entity("image"),
        }
    )


def electricity_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Smart plug + price step."""
    return vol.Schema(
        {
            _opt(CONF_POWER_ENTITY, defaults): _entity("sensor", "power"),
            _opt(CONF_ENERGY_ENTITY, defaults): _entity("sensor", "energy"),
            vol.Required(
                CONF_PRICE_ENTITY,
                description={"suggested_value": defaults.get(CONF_PRICE_ENTITY)},
            ): _entity(["sensor", "input_number", "number"]),
            vol.Required(
                CONF_INTEGRATION_METHOD,
                default=defaults.get(CONF_INTEGRATION_METHOD, DEFAULT_INTEGRATION_METHOD),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[str(m) for m in IntegrationMethod],
                    translation_key=CONF_INTEGRATION_METHOD,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


def filament_schema(defaults: dict[str, Any]) -> vol.Schema:
    """HaspelSync / Spoolman step (all optional)."""
    return vol.Schema(
        {
            _opt(CONF_HASPELSYNC_URL, defaults): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
            ),
            _opt(CONF_HASPELSYNC_API_KEY, defaults): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            _opt(CONF_SPOOLMAN_URL, defaults): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
            ),
            vol.Required(
                CONF_FILAMENT_FALLBACK,
                default=defaults.get(CONF_FILAMENT_FALLBACK, DEFAULT_FILAMENT_FALLBACK),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[str(f) for f in FilamentFallback],
                    translation_key=CONF_FILAMENT_FALLBACK,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


def behaviour_schema(defaults: dict[str, Any], hass_currency: str) -> vol.Schema:
    """Advanced options."""
    return vol.Schema(
        {
            vol.Required(
                CONF_UPDATE_INTERVAL, default=defaults.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_UPDATE_INTERVAL,
                    max=MAX_UPDATE_INTERVAL,
                    step=5,
                    unit_of_measurement="s",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_HISTORY_SIZE, default=defaults.get(CONF_HISTORY_SIZE, DEFAULT_HISTORY_SIZE)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_HISTORY_SIZE, max=MAX_HISTORY_SIZE, step=10, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(CONF_CURRENCY, default=defaults.get(CONF_CURRENCY) or hass_currency): selector.TextSelector(),
            vol.Required(
                CONF_GCODE_FOLDER, default=defaults.get(CONF_GCODE_FOLDER) or DEFAULT_GCODE_FOLDER
            ): selector.TextSelector(),
        }
    )


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    """Drop empty strings so cleared optional fields really disappear."""
    return {k: v for k, v in user_input.items() if v not in ("", None)}


def validate_electricity(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, str]:
    """At least one of power / energy, and supported units."""
    errors: dict[str, str] = {}
    if not data.get(CONF_POWER_ENTITY) and not data.get(CONF_ENERGY_ENTITY):
        errors["base"] = "power_or_energy_required"
    errors.update(
        validate_units(hass, data.get(CONF_POWER_ENTITY), data.get(CONF_ENERGY_ENTITY), data.get(CONF_PRICE_ENTITY))
    )
    return errors


async def validate_filament(
    hass: HomeAssistant, data: dict[str, Any]
) -> tuple[dict[str, str], list[HaspelsyncPrinter]]:
    """Reach HaspelSync / Spoolman when configured. Returns errors and printers."""
    errors: dict[str, str] = {}
    printers: list[HaspelsyncPrinter] = []
    session = async_get_clientsession(hass)

    if url := data.get(CONF_HASPELSYNC_URL):
        client = HaspelsyncClient(session, url, data.get(CONF_HASPELSYNC_API_KEY), timeout=HTTP_TIMEOUT)
        try:
            printers = await client.get_printers()
        except HaspelsyncAuthError:
            errors[CONF_HASPELSYNC_API_KEY] = "invalid_auth"
        except HaspelsyncHostRefusedError:
            errors[CONF_HASPELSYNC_URL] = "host_refused"
        except HaspelsyncError:
            errors[CONF_HASPELSYNC_URL] = "cannot_connect"
        else:
            if not printers:
                errors[CONF_HASPELSYNC_URL] = "no_printers"

    if url := data.get(CONF_SPOOLMAN_URL):
        try:
            await SpoolmanClient(session, url, timeout=HTTP_TIMEOUT).get_info()
        except SpoolmanError:
            errors[CONF_SPOOLMAN_URL] = "cannot_connect"

    return errors, printers


def _match_printer(printers: list[HaspelsyncPrinter], serial: str | None) -> str | None:
    if serial:
        for printer in printers:
            if printer.printer_id.upper() == serial.upper():
                return printer.printer_id
    if len(printers) == 1:
        return printers[0].printer_id
    return None


def _printer_select(printers: list[HaspelsyncPrinter], default: str | None) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_HASPELSYNC_PRINTER_ID, default=default or printers[0].printer_id
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=p.printer_id, label=f"{p.name} ({p.printer_id})")
                        for p in printers
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        }
    )


# --------------------------------------------------------------------------- #
# Config flow
# --------------------------------------------------------------------------- #
class PrintCostConfigFlow(ConfigFlow, domain=DOMAIN):
    """printer → Bambu entities → electricity → filament (optional)."""

    VERSION = 1

    def __init__(self) -> None:
        """Init."""
        self._data: dict[str, Any] = {}
        self._discovered: dict[str, str] = {}
        self._printers: list[HaspelsyncPrinter] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> PrintCostOptionsFlow:
        """Options flow."""
        return PrintCostOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick the Bambu Lab printer device."""
        if user_input is not None:
            device_id = user_input[CONF_PRINTER_DEVICE_ID]
            await self.async_set_unique_id(device_id)
            self._abort_if_unique_id_configured()
            self._data[CONF_PRINTER_DEVICE_ID] = device_id
            self._data[CONF_PRINTER_NAME] = bambu.printer_name(self.hass, device_id) or "Printer"
            if serial := bambu.printer_serial(self.hass, device_id):
                self._data[CONF_PRINTER_SERIAL] = serial
            self._discovered = bambu.discover_entities(self.hass, device_id)
            return await self.async_step_bambu()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PRINTER_DEVICE_ID): selector.DeviceSelector(
                        selector.DeviceSelectorConfig(integration=BAMBU_DOMAIN)
                    )
                }
            ),
        )

    async def async_step_bambu(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm / correct the discovered Bambu entities."""
        if user_input is not None:
            self._data.update(_clean(user_input))
            return await self.async_step_electricity()
        return self.async_show_form(
            step_id="bambu",
            data_schema=bambu_schema(self._discovered),
            description_placeholders={
                "printer": self._data.get(CONF_PRINTER_NAME, ""),
                "found": str(len(self._discovered)),
            },
        )

    async def async_step_electricity(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Smart plug and electricity price."""
        errors: dict[str, str] = {}
        if user_input is not None:
            cleaned = _clean(user_input)
            errors = validate_electricity(self.hass, cleaned)
            if not errors:
                self._data.update(cleaned)
                return await self.async_step_filament()
        return self.async_show_form(
            step_id="electricity",
            data_schema=electricity_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_filament(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Optional HaspelSync / Spoolman (v0.2: filament cost)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            cleaned = _clean(user_input)
            errors, self._printers = await validate_filament(self.hass, cleaned)
            if not errors:
                self._data.update(cleaned)
                if self._printers:
                    matched = _match_printer(self._printers, self._data.get(CONF_PRINTER_SERIAL))
                    if matched is None:
                        return await self.async_step_haspelsync_printer()
                    self._data[CONF_HASPELSYNC_PRINTER_ID] = matched
                return self._create()
        return self.async_show_form(
            step_id="filament",
            data_schema=filament_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_haspelsync_printer(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Several HaspelSync printers and none matches the serial."""
        if user_input is not None:
            self._data[CONF_HASPELSYNC_PRINTER_ID] = user_input[CONF_HASPELSYNC_PRINTER_ID]
            return self._create()
        return self.async_show_form(step_id="haspelsync_printer", data_schema=_printer_select(self._printers, None))

    def _create(self) -> ConfigFlowResult:
        return self.async_create_entry(title=f"{NAME} — {self._data[CONF_PRINTER_NAME]}", data=self._data)


# --------------------------------------------------------------------------- #
# Options flow
# --------------------------------------------------------------------------- #
class PrintCostOptionsFlow(OptionsFlow):
    """Change everything without removing the integration."""

    def __init__(self) -> None:
        """Init."""
        self._options: dict[str, Any] = {}
        self._printers: list[HaspelsyncPrinter] = []

    @property
    def _current(self) -> dict[str, Any]:
        return {**self.config_entry.data, **self.config_entry.options, **self._options}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Bambu entities."""
        if user_input is not None:
            self._options.update(_with_cleared(user_input, bambu_schema({})))
            return await self.async_step_electricity()
        return self.async_show_form(step_id="init", data_schema=bambu_schema(self._current))

    async def async_step_electricity(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Electricity."""
        errors: dict[str, str] = {}
        if user_input is not None:
            cleaned = _with_cleared(user_input, electricity_schema({}))
            errors = validate_electricity(self.hass, _clean(cleaned))
            if not errors:
                self._options.update(cleaned)
                return await self.async_step_filament()
        return self.async_show_form(step_id="electricity", data_schema=electricity_schema(self._current), errors=errors)

    async def async_step_filament(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """HaspelSync / Spoolman."""
        errors: dict[str, str] = {}
        if user_input is not None:
            cleaned = _with_cleared(user_input, filament_schema({}))
            errors, self._printers = await validate_filament(self.hass, _clean(cleaned))
            if not errors:
                self._options.update(cleaned)
                if self._printers:
                    current = self._current.get(CONF_HASPELSYNC_PRINTER_ID)
                    known = {p.printer_id for p in self._printers}
                    matched = (
                        current
                        if current in known
                        else _match_printer(self._printers, self._current.get(CONF_PRINTER_SERIAL))
                    )
                    if matched is None:
                        return await self.async_step_haspelsync_printer()
                    self._options[CONF_HASPELSYNC_PRINTER_ID] = matched
                return await self.async_step_behaviour()
        return self.async_show_form(step_id="filament", data_schema=filament_schema(self._current), errors=errors)

    async def async_step_haspelsync_printer(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick the HaspelSync printer."""
        if user_input is not None:
            self._options[CONF_HASPELSYNC_PRINTER_ID] = user_input[CONF_HASPELSYNC_PRINTER_ID]
            return await self.async_step_behaviour()
        return self.async_show_form(
            step_id="haspelsync_printer",
            data_schema=_printer_select(self._printers, self._current.get(CONF_HASPELSYNC_PRINTER_ID)),
        )

    async def async_step_behaviour(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Interval, history, currency."""
        if user_input is not None:
            self._options.update(
                {
                    CONF_UPDATE_INTERVAL: int(user_input[CONF_UPDATE_INTERVAL]),
                    CONF_HISTORY_SIZE: int(user_input[CONF_HISTORY_SIZE]),
                    CONF_CURRENCY: str(user_input[CONF_CURRENCY]).strip().upper(),
                    CONF_GCODE_FOLDER: str(user_input[CONF_GCODE_FOLDER]).strip().rstrip("/") or DEFAULT_GCODE_FOLDER,
                }
            )
            return self.async_create_entry(data=self._options)
        return self.async_show_form(
            step_id="behaviour", data_schema=behaviour_schema(self._current, self.hass.config.currency)
        )


def _with_cleared(user_input: dict[str, Any], schema: vol.Schema) -> dict[str, Any]:
    """Options: a cleared optional field must override the original data.

    HA omits cleared fields from ``user_input``; we store them as ``None`` so
    ``{**data, **options}`` really forgets them.
    """
    result: dict[str, Any] = {}
    for key in schema.schema:
        name = str(key)
        value = user_input.get(name)
        result[name] = None if value in ("", None) else value
    return result
