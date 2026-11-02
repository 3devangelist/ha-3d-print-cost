"""Is it still the same printer? Trust on first use.

A printer is the one the user added, or it is not. On the first good contact
its identity is remembered; if something else answers later at the same
address (a new device got the IP from DHCP, or something pretends to be the
printer), the integration notices, stops sending commands, never cuts the
power, and the admin decides in the printer's options.

What is remembered (one value per kind):

* ``tls_cert`` — Bambu Lab LAN: SHA-256 of the MQTT broker's TLS certificate,
  checked before the access code is sent;
* ``machine`` — Klipper: who Moonraker says the computer is (CPU serial
  number, else the network cards' MAC addresses, else the host name);
* ``ssh_host_key`` — the SSH host key used for the shutdown fallback, checked
  before the password is sent.

No Home Assistant import: unit-tested with plain values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

KIND_TLS = "tls_cert"
KIND_MACHINE = "machine"
KIND_SSH = "ssh_host_key"

# What the admin reads in the repair issue and the options flow.
KIND_LABELS = {
    KIND_TLS: "its TLS certificate",
    KIND_MACHINE: "its computer (Moonraker machine identity)",
    KIND_SSH: "its SSH host key",
}

# MAC addresses that do not identify anything
_NO_MAC = {"", "00:00:00:00:00:00"}


@dataclass(frozen=True, slots=True)
class IdentityChange:
    """Something at the printer's address no longer matches what was remembered."""

    kind: str
    old: str
    new: str | None  # None: different, but the new value is not known (SSH refused the key)


def klipper_machine_id(system_info: dict[str, Any] | None, hostname: str | None) -> str | None:
    """Identity of a Klipper computer from ``machine.system_info`` (and the host name as a last resort).

    ``cpu:<serial>`` when the CPU reports a serial number (Raspberry Pi and most
    boards), else ``mac:<mac1>,<mac2>`` (sorted), else ``host:<name>``; ``None``
    when nothing can be read.
    """
    info = system_info or {}
    cpu = info.get("cpu_info") if isinstance(info.get("cpu_info"), dict) else {}
    serial = str(cpu.get("serial_number") or "").strip().lower()
    if serial and serial.strip("0"):
        return f"cpu:{serial}"
    network = info.get("network") if isinstance(info.get("network"), dict) else {}
    macs = sorted(
        {
            mac
            for name, iface in network.items()
            if name != "lo"
            and isinstance(iface, dict)
            and (mac := str(iface.get("mac_address") or "").strip().lower()) not in _NO_MAC
        }
    )
    if macs:
        return "mac:" + ",".join(macs)
    name = (hostname or "").strip().lower()
    return f"host:{name}" if name else None


def same(kind: str, old: str, new: str) -> bool | None:
    """Whether two identities are the same printer; ``None`` when they cannot be compared."""
    if kind != KIND_MACHINE:
        return old == new
    old_kind, _, old_value = old.partition(":")
    new_kind, _, new_value = new.partition(":")
    if old_kind != new_kind:
        return None  # e.g. the CPU serial could not be read this time: no verdict
    if old_kind == "mac":
        # Network cards come and go (Wi-Fi, USB Ethernet): one card in common is the same computer.
        return bool(set(old_value.split(",")) & set(new_value.split(",")))
    return old_value == new_value


class PrinterIdentity:
    """What the printer was at first contact, and what changed since."""

    def __init__(self, known: dict[str, str] | None = None) -> None:
        self.known: dict[str, str] = {k: v for k, v in (known or {}).items() if isinstance(v, str) and v}
        self.changes: dict[str, IdentityChange] = {}

    @property
    def changed(self) -> bool:
        """Something no longer matches: no commands, no power cut."""
        return bool(self.changes)

    def check(self, kind: str, seen: str | None) -> tuple[bool, bool]:
        """Compare what the printer shows now with what was remembered.

        Returns ``(ok, learned)``: ``ok`` is False when it does not match;
        ``learned`` is True when this was the first contact (the caller saves).
        Nothing seen (``None``) is no verdict: the last verdict stays.
        """
        if not seen:
            return kind not in self.changes, False
        old = self.known.get(kind)
        if old is None:
            self.known[kind] = seen
            self.changes.pop(kind, None)
            return True, True
        verdict = same(kind, old, seen)
        if verdict is None:
            return kind not in self.changes, False
        if verdict:
            self.changes.pop(kind, None)  # the real printer is back
            return True, False
        self.changes[kind] = IdentityChange(kind, old, seen)
        return False, False

    def refuse(self, kind: str, new: str | None = None) -> None:
        """A change found elsewhere (SSH refused the remembered host key)."""
        old = self.known.get(kind, "")
        self.changes[kind] = IdentityChange(kind, old, new)

    def accept(self) -> None:
        """The admin says it is the same printer: remember the new identity (or learn it at next contact)."""
        for change in self.changes.values():
            if change.new:
                self.known[change.kind] = change.new
            else:
                self.known.pop(change.kind, None)
        self.changes.clear()

    def describe(self) -> str:
        """What changed, for the admin ("its TLS certificate")."""
        return ", ".join(KIND_LABELS.get(kind, kind) for kind in self.changes) or "nothing"
