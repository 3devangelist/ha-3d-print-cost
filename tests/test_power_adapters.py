"""SSH shutdown and the "is the computer still up?" check, without a network (0.8.0)."""

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.fdm_printer_manager.adapters import reach
from custom_components.fdm_printer_manager.adapters.ssh import (
    COSMOS_LOGIN,
    SshError,
    SshLogin,
    needs_cosmos_login,
    ssh_shutdown,
)

from .test_auto_off import CENTAURI_SHUTDOWN_500


def test_cosmos_is_recognised() -> None:
    assert needs_cosmos_login("cosmos", None)
    assert needs_cosmos_login("COSMOS ", None)
    assert needs_cosmos_login("centauri-carbon-1", None)  # COSMOS 26.10 and later
    assert needs_cosmos_login("centauri-carbon-2.local", None)
    assert not needs_cosmos_login("centauri-carbon", None)
    assert needs_cosmos_login(None, CENTAURI_SHUTDOWN_500)
    assert not needs_cosmos_login("Ender6", "HTTP 404 from http://x/machine/shutdown")
    assert not needs_cosmos_login(None, None)


def fake_asyncssh(conn: MagicMock | Exception) -> types.ModuleType:
    """A stand-in ``asyncssh`` module."""
    mod = types.ModuleType("asyncssh")

    class Error(Exception):
        pass

    class DisconnectError(Error):
        pass

    class PermissionDenied(DisconnectError):
        pass

    class ConnectionLost(DisconnectError):
        pass

    class KeyImportError(ValueError):
        pass

    mod.Error = Error
    mod.DisconnectError = DisconnectError
    mod.PermissionDenied = PermissionDenied
    mod.ConnectionLost = ConnectionLost
    mod.KeyImportError = KeyImportError
    mod.import_private_key = MagicMock(return_value="KEY")
    if isinstance(conn, Exception):
        mod.connect = AsyncMock(side_effect=conn)
    else:
        mod.connect = AsyncMock(return_value=conn)
    return mod


def connection(run_error: Exception | None = None) -> MagicMock:
    conn = MagicMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    conn.run = AsyncMock(side_effect=run_error) if run_error else AsyncMock(return_value=MagicMock(exit_status=0))
    return conn


async def test_cosmos_login_runs_poweroff_once_as_root_without_password() -> None:
    conn = connection()
    mod = fake_asyncssh(conn)
    with patch.dict(sys.modules, {"asyncssh": mod}):
        await ssh_shutdown("192.168.1.95", COSMOS_LOGIN)
    kwargs = mod.connect.await_args.kwargs
    assert mod.connect.await_args.args == ("192.168.1.95",)
    assert kwargs == {"username": "root", "known_hosts": None, "port": 22, "client_keys": None, "password": ""}
    conn.run.assert_awaited_once_with("poweroff", check=False)


async def test_the_connection_dropping_during_poweroff_is_fine() -> None:
    mod = fake_asyncssh(MagicMock())
    conn = connection(run_error=mod.ConnectionLost("gone"))
    mod.connect = AsyncMock(return_value=conn)
    with patch.dict(sys.modules, {"asyncssh": mod}):
        await ssh_shutdown("192.168.1.95", COSMOS_LOGIN)  # no error: the ping decides


async def test_a_refused_login_is_an_error() -> None:
    mod = fake_asyncssh(MagicMock())
    mod.connect = AsyncMock(side_effect=mod.PermissionDenied("no"))
    with patch.dict(sys.modules, {"asyncssh": mod}), pytest.raises(SshError, match="refused"):
        await ssh_shutdown("192.168.1.95", SshLogin(username="pi", password="wrong"))


async def test_unreachable_is_an_error() -> None:
    mod = fake_asyncssh(OSError("no route"))
    with patch.dict(sys.modules, {"asyncssh": mod}), pytest.raises(SshError):
        await ssh_shutdown("192.168.1.95", COSMOS_LOGIN)


async def test_password_and_key_are_passed() -> None:
    conn = connection()
    mod = fake_asyncssh(conn)
    with patch.dict(sys.modules, {"asyncssh": mod}):
        await ssh_shutdown("h", SshLogin(username="pi", password="pw", private_key="-----KEY-----"))
    kwargs = mod.connect.await_args.kwargs
    assert kwargs["password"] == "pw"
    assert kwargs["client_keys"] == ["KEY"]


async def test_missing_asyncssh_is_an_error() -> None:
    with patch.dict(sys.modules, {"asyncssh": None}), pytest.raises(SshError, match="not installed"):
        await ssh_shutdown("h", COSMOS_LOGIN)


# --------------------------------------------------------------------------- #
# reach
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def no_icmp():
    with patch.object(reach, "_icmp_alive", AsyncMock(return_value=None)):
        yield


async def test_refused_connection_means_the_computer_is_up() -> None:
    with patch("asyncio.open_connection", AsyncMock(side_effect=ConnectionRefusedError())):
        assert await reach.host_is_up("192.168.1.95", 80)


async def test_timeout_means_the_computer_is_off() -> None:
    with patch("asyncio.open_connection", AsyncMock(side_effect=TimeoutError())):
        assert not await reach.host_is_up("192.168.1.95", 80)


async def test_no_route_means_the_computer_is_off() -> None:
    with patch("asyncio.open_connection", AsyncMock(side_effect=OSError(113, "No route to host"))):
        assert not await reach.host_is_up("192.168.1.95", 80)


async def test_a_ping_answer_is_enough() -> None:
    with (
        patch.object(reach, "_icmp_alive", AsyncMock(return_value=True)),
        patch("asyncio.open_connection", AsyncMock(side_effect=TimeoutError())),
    ):
        assert await reach.host_is_up("192.168.1.95", 80)


async def test_no_ping_answer_still_asks_tcp() -> None:
    writer = MagicMock()
    with (
        patch.object(reach, "_icmp_alive", AsyncMock(return_value=False)),
        patch("asyncio.open_connection", AsyncMock(return_value=(MagicMock(), writer))),
    ):
        assert await reach.host_is_up("192.168.1.95", 80)
    writer.close.assert_called_once()
