"""Config flow (UI only, no YAML) and options flow.

Add printer: printer type → connection (or Bambu device + entities) → smart plug.
"Configure" on the entry edits the same things without deleting the printer.
"""

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
from .adapters.moonraker import (
    DEFAULT_PORT,
    MoonrakerAuthError,
    MoonrakerClient,
    MoonrakerError,
    default_web_url,
)
from .const import (
    BAMBU_DOMAIN,
    CONF_API_KEY,
    CONF_COVER_IMAGE_ENTITY,
    CONF_CURRENCY,
    CONF_ENERGY_ENTITY,
    CONF_HOST,
    CONF_INTEGRATION_METHOD,
    CONF_JOB_NAME_ENTITY,
    CONF_PANEL,
    CONF_PORT,
    CONF_POWER_ENTITY,
    CONF_PRICE_ENTITY,
    CONF_PRINT_STATUS_ENTITY,
    CONF_PRINTER_DEVICE_ID,
    CONF_PRINTER_NAME,
    CONF_PRINTER_SERIAL,
    CONF_PRINTER_TYPE,
    CONF_PROGRESS_ENTITY,
    CONF_REMAINING_TIME_ENTITY,
    CONF_SWITCH_ENTITY,
    CONF_UPDATE_INTERVAL,
    CONF_WEB_URL,
    DEFAULT_INTEGRATION_METHOD,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    HTTP_TIMEOUT,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
    IntegrationMethod,
    PrinterType,
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
            _opt(CONF_REMAINING_TIME_ENTITY, defaults): _entity("sensor"),
            _opt(CONF_COVER_IMAGE_ENTITY, defaults): _entity("image"),
        }
    )


def klipper_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Connection to a Klipper printer (Moonraker)."""
    return vol.Schema(
        {
            _opt(CONF_PRINTER_NAME, defaults): selector.TextSelector(),
            vol.Required(CONF_HOST, description={"suggested_value": defaults.get(CONF_HOST)}): selector.TextSelector(),
            vol.Required(CONF_PORT, default=int(defaults.get(CONF_PORT) or DEFAULT_PORT)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=65535, step=1, mode=selector.NumberSelectorMode.BOX)
            ),
            _opt(CONF_API_KEY, defaults): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            _opt(CONF_WEB_URL, defaults): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
            ),
        }
    )


def klipper_add_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Adding a Klipper printer: only the IP address (``host`` or ``host:port``)."""
    return vol.Schema(
        {vol.Required(CONF_HOST, description={"suggested_value": defaults.get(CONF_HOST)}): selector.TextSelector()}
    )


def split_host_port(value: str) -> tuple[str, int | None]:
    """``192.168.1.89:7126`` → (``192.168.1.89``, 7126). IPv6 must be in brackets to carry a port."""
    value = value.strip()
    for prefix in ("http://", "https://"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
    value = value.rstrip("/")
    if value.startswith("[") and "]:" in value:
        host, _, port = value[1:].partition("]:")
        return host, int(port) if port.isdigit() else None
    if value.count(":") == 1:
        host, _, port = value.partition(":")
        if port.isdigit():
            return host, int(port)
    return value.strip("[]"), None


async def validate_klipper(hass: HomeAssistant, data: dict[str, Any]) -> tuple[dict[str, str], str | None]:
    """Reach Moonraker. Returns errors and the printer's hostname (default name)."""
    client = MoonrakerClient(
        async_get_clientsession(hass),
        data[CONF_HOST],
        int(data.get(CONF_PORT) or DEFAULT_PORT),
        data.get(CONF_API_KEY),
        timeout=HTTP_TIMEOUT,
    )
    try:
        await client.server_info()
    except MoonrakerAuthError:
        return {CONF_API_KEY: "invalid_auth"}, None
    except MoonrakerError as err:
        _LOGGER.debug("Moonraker not reachable: %s", err)
        return {CONF_HOST: "cannot_connect"}, None
    return {}, await client.hostname()


def normalise_klipper(data: dict[str, Any], hostname: str | None) -> dict[str, Any]:
    """Fill the defaults the user left empty (name, port, web interface)."""
    result = dict(data)
    result[CONF_HOST] = str(data[CONF_HOST]).strip()
    result[CONF_PORT] = int(data.get(CONF_PORT) or DEFAULT_PORT)
    result[CONF_PRINTER_NAME] = (data.get(CONF_PRINTER_NAME) or "").strip() or hostname or result[CONF_HOST]
    result[CONF_WEB_URL] = (data.get(CONF_WEB_URL) or "").strip() or default_web_url(result[CONF_HOST])
    return result


def electricity_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Smart plug step: the switch, and optionally its meter and the price."""
    return vol.Schema(
        {
            _opt(CONF_SWITCH_ENTITY, defaults): _entity(["switch", "light", "input_boolean"]),
            _opt(CONF_POWER_ENTITY, defaults): _entity("sensor", "power"),
            _opt(CONF_ENERGY_ENTITY, defaults): _entity("sensor", "energy"),
            _opt(CONF_PRICE_ENTITY, defaults): _entity(["sensor", "input_number", "number"]),
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
            vol.Required(CONF_CURRENCY, default=defaults.get(CONF_CURRENCY) or hass_currency): selector.TextSelector(),
        }
    )


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    """Drop empty strings so cleared optional fields really disappear."""
    return {k: v for k, v in user_input.items() if v not in ("", None)}


def validate_electricity(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, str]:
    """A price is needed only when the plug measures energy; units must be supported."""
    errors: dict[str, str] = {}
    if (data.get(CONF_POWER_ENTITY) or data.get(CONF_ENERGY_ENTITY)) and not data.get(CONF_PRICE_ENTITY):
        errors[CONF_PRICE_ENTITY] = "price_required"
    errors.update(
        validate_units(hass, data.get(CONF_POWER_ENTITY), data.get(CONF_ENERGY_ENTITY), data.get(CONF_PRICE_ENTITY))
    )
    return errors


# --------------------------------------------------------------------------- #
# Config flow
# --------------------------------------------------------------------------- #
class PrintCostConfigFlow(ConfigFlow, domain=DOMAIN):
    """type → connection (Klipper) or device + entities (Bambu) → plug."""

    VERSION = 1

    def __init__(self) -> None:
        """Init."""
        self._data: dict[str, Any] = {}
        self._discovered: dict[str, str] = {}
        self._klipper: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> PrintCostOptionsFlow:
        """Options flow."""
        return PrintCostOptionsFlow()

    @classmethod
    @callback
    def async_supports_options_flow(cls, config_entry: ConfigEntry) -> bool:
        """The panel-only entry has nothing to configure."""
        return not config_entry.data.get(CONF_PANEL)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """What kind of printer (or just the sidebar panel, until it exists)."""
        options = ["klipper", "bambu_device"]
        if not any(e.data.get(CONF_PANEL) for e in self._async_current_entries(include_ignore=False)):
            options.append("panel")
        return self.async_show_menu(step_id="user", menu_options=options)

    async def async_step_panel(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Only add the "FDM Printers" sidebar panel; printers are then added from there."""
        await self.async_set_unique_id(CONF_PANEL)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="FDM Printers panel", data={CONF_PANEL: True})

    async def async_step_klipper(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Klipper: only the IP address. Port, name and web interface are filled in automatically."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host, port = split_host_port(user_input[CONF_HOST])
            self._klipper = {CONF_HOST: host, CONF_PORT: port or DEFAULT_PORT}
            await self.async_set_unique_id(f"klipper_{host.lower()}_{self._klipper[CONF_PORT]}")
            self._abort_if_unique_id_configured()
            errors, hostname = await validate_klipper(self.hass, self._klipper)
            if errors.get(CONF_API_KEY):
                # Moonraker wants a key: ask for it, and only for it.
                return await self.async_step_klipper_auth()
            if not errors:
                return await self._async_klipper_done(hostname)
        return self.async_show_form(step_id="klipper", data_schema=klipper_add_schema(user_input or {}), errors=errors)

    async def async_step_klipper_auth(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Shown only when Moonraker refuses the connection without an API key."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._klipper[CONF_API_KEY] = user_input[CONF_API_KEY]
            errors, hostname = await validate_klipper(self.hass, self._klipper)
            if not errors:
                return await self._async_klipper_done(hostname)
            errors = {CONF_API_KEY: errors.get(CONF_API_KEY, "cannot_connect")}
        return self.async_show_form(
            step_id="klipper_auth",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    )
                }
            ),
            description_placeholders={"host": self._klipper[CONF_HOST]},
            errors=errors,
        )

    async def _async_klipper_done(self, hostname: str | None) -> ConfigFlowResult:
        self._data.update(normalise_klipper(self._klipper, hostname))
        self._data[CONF_PRINTER_TYPE] = PrinterType.KLIPPER
        return await self.async_step_electricity()

    async def async_step_bambu_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Bambu Lab: pick the printer from the ha-bambulab integration."""
        if user_input is not None:
            device_id = user_input[CONF_PRINTER_DEVICE_ID]
            await self.async_set_unique_id(device_id)
            self._abort_if_unique_id_configured()
            self._data[CONF_PRINTER_DEVICE_ID] = device_id
            self._data[CONF_PRINTER_TYPE] = PrinterType.BAMBU
            self._data[CONF_PRINTER_NAME] = bambu.printer_name(self.hass, device_id) or "Printer"
            if serial := bambu.printer_serial(self.hass, device_id):
                self._data[CONF_PRINTER_SERIAL] = serial
            self._discovered = bambu.discover_entities(self.hass, device_id)
            return await self.async_step_bambu()

        return self.async_show_form(
            step_id="bambu_device",
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
                return self._create()
        return self.async_show_form(
            step_id="electricity",
            data_schema=electricity_schema(user_input or {}),
            errors=errors,
        )

    def _create(self) -> ConfigFlowResult:
        return self.async_create_entry(title=self._data[CONF_PRINTER_NAME], data=self._data)


# --------------------------------------------------------------------------- #
# Options flow
# --------------------------------------------------------------------------- #
class PrintCostOptionsFlow(OptionsFlow):
    """Change everything without removing the integration."""

    def __init__(self) -> None:
        """Init."""
        self._options: dict[str, Any] = {}

    @property
    def _current(self) -> dict[str, Any]:
        return {**self.config_entry.data, **self.config_entry.options, **self._options}

    @property
    def _is_klipper(self) -> bool:
        return self._current.get(CONF_PRINTER_TYPE) == PrinterType.KLIPPER

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Printer connection (Klipper) or entities (Bambu)."""
        if self._is_klipper:
            return await self.async_step_klipper()
        if user_input is not None:
            self._options.update(_with_cleared(user_input, bambu_schema({})))
            return await self.async_step_electricity()
        return self.async_show_form(step_id="init", data_schema=bambu_schema(self._current))

    async def async_step_klipper(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change IP, port, API key, web interface or name (history is kept)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            cleaned = _clean(user_input)
            old_host = self._current.get(CONF_HOST)
            if old_host and cleaned.get(CONF_WEB_URL) == default_web_url(old_host):
                cleaned.pop(CONF_WEB_URL)  # still the default: follow the new IP
            errors, hostname = await validate_klipper(self.hass, cleaned)
            if not errors:
                data = normalise_klipper(cleaned, hostname)
                # A cleared API key must override the one stored in the entry data.
                self._options.update({str(k): data.get(str(k)) for k in klipper_schema({}).schema})
                if data[CONF_PRINTER_NAME] != self.config_entry.title:
                    self.hass.config_entries.async_update_entry(self.config_entry, title=data[CONF_PRINTER_NAME])
                return await self.async_step_electricity()
        return self.async_show_form(
            step_id="klipper", data_schema=klipper_schema(user_input or self._current), errors=errors
        )

    async def async_step_electricity(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Electricity."""
        errors: dict[str, str] = {}
        if user_input is not None:
            cleaned = _with_cleared(user_input, electricity_schema({}))
            errors = validate_electricity(self.hass, _clean(cleaned))
            if not errors:
                self._options.update(cleaned)
                return await self.async_step_behaviour()
        return self.async_show_form(step_id="electricity", data_schema=electricity_schema(self._current), errors=errors)

    async def async_step_behaviour(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Interval, currency."""
        if user_input is not None:
            self._options.update(
                {
                    CONF_UPDATE_INTERVAL: int(user_input[CONF_UPDATE_INTERVAL]),
                    CONF_CURRENCY: str(user_input[CONF_CURRENCY]).strip().upper(),
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
