"""The "Google Sheets" button: create the tab and turn the log on for every printer."""

import sys
import types
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockModule, mock_integration

from custom_components.fdm_printer_manager import sheets
from custom_components.fdm_printer_manager.const import DOMAIN
from custom_components.fdm_printer_manager.services import async_setup_services
from custom_components.fdm_printer_manager.sheets import HEADER, prepare_worksheet

from .test_klipper_integration import printer, setup_ender  # noqa: F401 - printer is a fixture

PREPARE = "custom_components.fdm_printer_manager.sheets.prepare_worksheet"
FOUND = {"spreadsheet_title": "Home Assistant", "created": True, "header": True, "gid": 42}


def google_entry(
    hass: HomeAssistant, *, loaded: bool = True, unique_id: str = "SHEET123", title: str = "Google Sheets"
) -> MockConfigEntry:
    """A google_sheets entry; its unique ID is the spreadsheet ID (the real integration needs gspread: faked)."""
    mock_integration(hass, MockModule("google_sheets"))
    entry = MockConfigEntry(
        domain="google_sheets",
        title=title,
        unique_id=unique_id,
        data={"token": {"access_token": "token-1", "scope": "https://www.googleapis.com/auth/drive.file"}},
    )
    entry.add_to_hass(hass)
    if loaded:
        entry.mock_state(hass, ConfigEntryState.LOADED)
        entry.runtime_data = Mock(async_ensure_token_valid=AsyncMock())
    return entry


def other_printer(hass: HomeAssistant) -> MockConfigEntry:
    """A second printer, not loaded, with options of its own that must survive."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Centauri Carbon",
        unique_id="klipper_192.168.1.90_7125",
        data={"printer_type": "klipper", "printer_name": "Centauri Carbon", "host": "192.168.1.90", "port": 7125},
        options={"todo_entity": "todo.shopping", "stall_minutes": 30, "sheets_worksheet": "Old"},
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, **data):
    async_setup_services(hass)  # registered by async_setup; here without loading any printer
    return await hass.services.async_call(DOMAIN, "sheets_setup", data, blocking=True, return_response=True)


# --------------------------------------------------------------------------- #
# The tab, through gspread (faked: it comes with the google_sheets integration)
# --------------------------------------------------------------------------- #
class _NotFound(Exception):
    pass


class _Worksheet:
    def __init__(self, title: str, header: list[str] | None = None) -> None:
        self.title = title
        self.id = 7 if header is not None else 99
        self.header = header or []
        self.appended: list[list[str]] = []
        self.frozen = 0

    def row_values(self, row: int) -> list[str]:
        return list(self.header)

    def append_row(self, values, value_input_option=None) -> None:
        self.appended.append(values)

    def freeze(self, rows: int = 0) -> None:
        self.frozen = rows


class _Spreadsheet:
    title = "Home Assistant"

    def __init__(self, tabs: dict[str, _Worksheet]) -> None:
        self.tabs = tabs
        self.added: list[tuple[str, int, int]] = []

    def worksheet(self, title: str) -> _Worksheet:
        if title not in self.tabs:
            raise _NotFound(title)
        return self.tabs[title]

    def add_worksheet(self, title: str, rows: int, cols: int) -> _Worksheet:
        self.added.append((title, rows, cols))
        self.tabs[title] = _Worksheet(title)
        return self.tabs[title]


@pytest.fixture
def fake_gspread():
    """Fake gspread / google-auth modules; yields the spreadsheet the client opens."""
    book = _Spreadsheet({"Sheet1": _Worksheet("Sheet1", [])})
    opened: list[tuple[str, str]] = []

    class Client:
        def __init__(self, credentials) -> None:
            self.token = credentials.token

        def open_by_key(self, key: str) -> _Spreadsheet:
            opened.append((self.token, key))
            return book

    gspread = types.ModuleType("gspread")
    gspread.Client = Client
    exceptions = types.ModuleType("gspread.exceptions")
    exceptions.WorksheetNotFound = _NotFound
    google = types.ModuleType("google")
    oauth2 = types.ModuleType("google.oauth2")
    credentials = types.ModuleType("google.oauth2.credentials")
    credentials.Credentials = lambda token: types.SimpleNamespace(token=token)
    modules = {
        "gspread": gspread,
        "gspread.exceptions": exceptions,
        "google": google,
        "google.oauth2": oauth2,
        "google.oauth2.credentials": credentials,
    }
    with patch.dict(sys.modules, modules):
        book.opened = opened
        yield book


def test_creates_the_missing_tab_with_its_header(fake_gspread) -> None:
    found = prepare_worksheet("token-1", "SHEET123", "Prints")
    assert fake_gspread.opened == [("token-1", "SHEET123")]
    assert fake_gspread.added == [("Prints", sheets.NEW_TAB_ROWS, len(HEADER))]
    tab = fake_gspread.tabs["Prints"]
    assert tab.appended == [list(HEADER)]
    assert HEADER[0] == "created"
    assert tab.frozen == 1
    assert found == {"spreadsheet_title": "Home Assistant", "created": True, "header": True, "gid": 99}


def test_existing_tab_is_left_as_it_is(fake_gspread) -> None:
    fake_gspread.tabs["Prints"] = _Worksheet("Prints", ["created", "impressora", "ficheiro"])
    found = prepare_worksheet("token-1", "SHEET123", "Prints")
    assert fake_gspread.added == []
    assert fake_gspread.tabs["Prints"].appended == []
    assert found["created"] is False
    assert found["header"] is False
    assert found["gid"] == 7


def test_existing_empty_tab_gets_the_header(fake_gspread) -> None:
    found = prepare_worksheet("token-1", "SHEET123", "Sheet1")
    assert fake_gspread.added == []
    assert fake_gspread.tabs["Sheet1"].appended == [list(HEADER)]
    assert (found["created"], found["header"]) == (False, True)


# --------------------------------------------------------------------------- #
# The action
# --------------------------------------------------------------------------- #
async def test_setup_applies_to_all_printers_and_keeps_other_options(
    hass: HomeAssistant,
    printer,  # noqa: F811
    panel_entry,
) -> None:
    google = google_entry(hass)
    ender = await setup_ender(hass)
    centauri = other_printer(hass)

    with patch(PREPARE, return_value=FOUND) as prepare:
        result = await _setup(hass, config_entry=google.entry_id, worksheet=" Prints ")
        await hass.async_block_till_done()

    prepare.assert_called_once_with("token-1", "SHEET123", "Prints")
    google.runtime_data.async_ensure_token_valid.assert_awaited_once()
    for entry in (ender, centauri):
        assert entry.options["sheets_config_entry"] == google.entry_id
        assert entry.options["sheets_worksheet"] == "Prints"
    assert centauri.options["todo_entity"] == "todo.shopping"
    assert centauri.options["stall_minutes"] == 30
    assert "sheets_config_entry" not in panel_entry.options
    assert result["url"] == "https://docs.google.com/spreadsheets/d/SHEET123"
    assert result["worksheet_url"] == "https://docs.google.com/spreadsheets/d/SHEET123/edit#gid=42"
    assert result["created"] is True
    assert sorted(result["printers"]) == ["Centauri Carbon", "Ender 6"]
    # The Ender was reloaded with the new options and logs to the sheet.
    assert ender.state is ConfigEntryState.LOADED
    assert ender.runtime_data.sheets.config_entry == google.entry_id


async def test_setup_defaults_first_entry_and_prints(hass: HomeAssistant) -> None:
    first = google_entry(hass, unique_id="FIRST")
    google_entry(hass, unique_id="SECOND")
    centauri = other_printer(hass)
    with patch(PREPARE, return_value={**FOUND, "created": False}) as prepare:
        result = await _setup(hass)
    prepare.assert_called_once_with("token-1", "FIRST", "Prints")
    assert centauri.options["sheets_config_entry"] == first.entry_id
    assert centauri.options["sheets_worksheet"] == "Prints"
    assert result["created"] is False


async def test_setup_only_one_printer(hass: HomeAssistant) -> None:
    google = google_entry(hass)
    centauri = other_printer(hass)
    lonely = MockConfigEntry(domain=DOMAIN, title="A1 mini", data={"printer_type": "bambu_lan"})
    lonely.add_to_hass(hass)
    with patch(PREPARE, return_value=FOUND):
        result = await _setup(hass, config_entry=google.entry_id, entry_id=centauri.entry_id)
    assert result["printers"] == ["Centauri Carbon"]
    assert centauri.options["sheets_config_entry"] == google.entry_id
    assert "sheets_config_entry" not in lonely.options


async def test_no_google_sheets_entry_is_a_clear_error(hass: HomeAssistant) -> None:
    centauri = other_printer(hass)
    with patch(PREPARE) as prepare, pytest.raises(ServiceValidationError, match="Google Sheets is not set up"):
        await _setup(hass)
    prepare.assert_not_called()
    assert "sheets_config_entry" not in centauri.options


async def test_google_sheets_entry_not_working(hass: HomeAssistant) -> None:
    google = google_entry(hass, loaded=False)
    other_printer(hass)
    with patch(PREPARE) as prepare, pytest.raises(ServiceValidationError, match="sign in with Google again"):
        await _setup(hass, config_entry=google.entry_id)
    prepare.assert_not_called()


async def test_wrong_entry_is_refused(hass: HomeAssistant) -> None:
    google_entry(hass)
    centauri = other_printer(hass)
    with pytest.raises(ServiceValidationError, match="not a Google Sheets"):
        await _setup(hass, config_entry=centauri.entry_id)


async def test_google_error_changes_nothing(hass: HomeAssistant) -> None:
    google = google_entry(hass)
    centauri = other_printer(hass)
    with (
        patch(PREPARE, side_effect=RuntimeError("APIError: 403 insufficient permissions")),
        pytest.raises(HomeAssistantError, match="403 insufficient permissions"),
    ):
        await _setup(hass, config_entry=google.entry_id)
    assert "sheets_config_entry" not in centauri.options
    assert centauri.options["sheets_worksheet"] == "Old"


async def test_no_printer_yet(hass: HomeAssistant, panel_entry) -> None:
    google_entry(hass)
    with patch(PREPARE) as prepare, pytest.raises(ServiceValidationError, match="no printer"):
        await _setup(hass)
    prepare.assert_not_called()


# --------------------------------------------------------------------------- #
# Panel (websocket)
# --------------------------------------------------------------------------- #
async def test_panel_status_setup_and_off(hass: HomeAssistant, hass_ws_client, printer, panel_entry) -> None:  # noqa: F811
    ender = await setup_ender(hass)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/sheets_setup"})
    reply = await client.receive_json()
    assert not reply["success"]
    assert "Google Sheets is not set up" in reply["error"]["message"]

    google = google_entry(hass, title="Prints log")
    await client.send_json_auto_id({"type": f"{DOMAIN}/sheets_status"})
    status = (await client.receive_json())["result"]
    assert status["entries"] == [
        {
            "entry_id": google.entry_id,
            "title": "Prints log",
            "loaded": True,
            "url": "https://docs.google.com/spreadsheets/d/SHEET123",
        }
    ]
    assert status["printers"] == [
        {"entry_id": ender.entry_id, "title": "Ender 6", "sheets_entry": None, "worksheet": None}
    ]

    with patch(PREPARE, return_value=FOUND):
        await client.send_json_auto_id({"type": f"{DOMAIN}/sheets_setup", "config_entry": google.entry_id})
        reply = await client.receive_json()
        await hass.async_block_till_done()
    assert reply["success"]
    assert reply["result"]["worksheet"] == "Prints"

    await client.send_json_auto_id({"type": f"{DOMAIN}/sheets_status"})
    status = (await client.receive_json())["result"]
    assert status["printers"][0]["sheets_entry"] == google.entry_id
    assert status["printers"][0]["worksheet"] == "Prints"

    await client.send_json_auto_id({"type": f"{DOMAIN}/sheets_off"})
    reply = await client.receive_json()
    await hass.async_block_till_done()
    assert reply["result"] == {"stopped": ["Ender 6"]}
    assert ender.options["sheets_config_entry"] is None
    assert ender.options["sheets_worksheet"] == "Prints"
    assert ender.runtime_data.sheets.enabled is False
