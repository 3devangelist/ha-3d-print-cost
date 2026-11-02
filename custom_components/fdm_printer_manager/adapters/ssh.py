"""Shut a printer's computer down over SSH.

The fallback when Moonraker's ``/machine/shutdown`` fails or is missing.

* The Elegoo Centauri Carbon ("cosmos" firmware) answers that request with
  HTTP 500 "Error running shell command: 'sudo systemctl poweroff'" and stays
  on, but lets ``root`` in over SSH without a password, where ``poweroff``
  works (tested: the printer stops answering pings about 10 s later). It is
  recognised by its host name or that error and needs no setup
  (:data:`COSMOS_LOGIN`).
* Any other printer: the user can give a login in the integration options.

Host keys are trusted on first use: the first connection learns the printer's
host key (the caller remembers it, see identity.py) and every later one
accepts only that key, so the password never goes to another machine that got
the printer's IP address. A changed key (new firmware, or another device) is
refused before logging in: :class:`SshHostKeyChanged`. The password is never
logged. ``asyncssh`` is imported only when it is used.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

SSH_PORT = 22
SSH_TIMEOUT = 10  # seconds to connect and send the command
DEFAULT_SSH_USERNAME = "root"
DEFAULT_SSH_COMMAND = "poweroff"

# Moonraker's answer when its shutdown runs a command the firmware does not allow
SHELL_COMMAND_ERROR = "Error running shell command"
COSMOS_HOSTNAME = "cosmos"
# COSMOS 26.10 and later: "centauri-carbon-1", "centauri-carbon-2"...
COSMOS_HOSTNAME_PATTERN = re.compile(r"centauri-carbon-\d+")


class SshError(Exception):
    """The SSH shutdown could not be sent."""


class SshHostKeyChanged(SshError):
    """The printer's SSH host key is not the remembered one: nothing was sent."""


@dataclass(frozen=True, slots=True)
class SshLogin:
    """What the user typed in the options."""

    username: str
    password: str | None = None
    private_key: str | None = None
    command: str = DEFAULT_SSH_COMMAND

    def __repr__(self) -> str:  # never show the secrets
        return f"SshLogin(username={self.username!r}, command={self.command!r})"


# Elegoo Centauri Carbon: root, no password, poweroff
COSMOS_LOGIN = SshLogin(username=DEFAULT_SSH_USERNAME)


def needs_cosmos_login(hostname: str | None, moonraker_error: str | None) -> bool:
    """Whether this looks like the Elegoo "cosmos" firmware (host name, or its shutdown error)."""
    key = (hostname or "").strip().lower().removesuffix(".local")
    if key == COSMOS_HOSTNAME or COSMOS_HOSTNAME_PATTERN.fullmatch(key):
        return True
    return moonraker_error is not None and SHELL_COMMAND_ERROR in moonraker_error


async def ssh_shutdown(host: str, login: SshLogin, host_key: str | None = None) -> str | None:
    """Run the shutdown command. The connection dropping while it runs counts as success.

    ``host_key``: the printer's remembered host key (OpenSSH public key line);
    only that key is accepted. ``None``: first use, any key is accepted and
    returned, for the caller to remember.
    """
    try:
        import asyncssh  # noqa: PLC0415 - only needed when SSH is configured
    except ImportError as err:
        raise SshError("asyncssh is not installed") from err

    # Only the key the user gave: never Home Assistant's own ~/.ssh keys. Without a
    # password or key, an empty password is offered after the "none" method
    # (the Centauri lets root in with either).
    options: dict = {"username": login.username, "known_hosts": None, "port": SSH_PORT, "client_keys": None}
    if host_key:
        try:
            options["known_hosts"] = ([asyncssh.import_public_key(host_key)], [], [])
        except (asyncssh.KeyImportError, ValueError) as err:
            raise SshHostKeyChanged("the remembered host key could not be read") from err
    if login.private_key:
        try:
            options["client_keys"] = [asyncssh.import_private_key(login.private_key)]
        except (asyncssh.KeyImportError, ValueError) as err:
            raise SshError("the private key could not be read") from err
    if login.password:
        options["password"] = login.password
    elif not login.private_key:
        options["password"] = ""
    try:
        conn = await asyncio.wait_for(asyncssh.connect(host, **options), SSH_TIMEOUT)
    except asyncssh.HostKeyNotVerifiable as err:
        raise SshHostKeyChanged("the printer's SSH host key changed") from err
    except asyncssh.PermissionDenied as err:
        raise SshError("SSH login refused") from err
    except TimeoutError as err:
        raise SshError("SSH timed out") from err
    except (OSError, asyncssh.Error) as err:
        raise SshError(f"SSH failed: {type(err).__name__}") from err
    seen_key: str | None = None
    try:
        server_key = conn.get_server_host_key()
        if server_key is not None:
            seen_key = server_key.export_public_key("openssh").decode().strip()
    except (AttributeError, ValueError, asyncssh.Error):
        seen_key = None
    # One command, no pty, no file transfer. It runs in the login shell of the
    # remote user (the Centauri runs BusyBox), so it is whatever the admin typed
    # in the options, nothing else is added. Its exit code is not trusted either:
    # the connection often drops half-way through a poweroff. Success is the
    # printer no longer answering, which the caller checks.
    try:
        async with conn:
            await asyncio.wait_for(conn.run(login.command, check=False), SSH_TIMEOUT)
    except (TimeoutError, OSError, asyncssh.Error):
        pass  # the computer went down while answering: expected
    _LOGGER.info("%s: shutdown command sent over SSH", host)
    return host_key or seen_key
