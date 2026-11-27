"""Resolve the LAN interface named in config to one address."""
from __future__ import annotations

import os
import socket
import subprocess
import sys


def _ip_literal(value: str) -> bool:
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, value)
            return True
        except OSError:
            continue
    return False


def _linux_ipv4(name: str) -> str:
    import fcntl
    import struct

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = fcntl.ioctl(
            sock.fileno(),
            0x8915,
            struct.pack("256s", name.encode("utf-8")[:15]),
        )
    except OSError as exc:
        raise ValueError(f"interface {name} has no IPv4 address") from exc
    finally:
        sock.close()
    return socket.inet_ntoa(packed[20:24])


def _windows_ipv4(name: str) -> str:
    script = (
        "Get-NetIPAddress -InterfaceAlias $env:CENTRAL_IFACE -AddressFamily IPv4 "
        "| Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } "
        "| Select-Object -ExpandProperty IPAddress -First 1"
    )
    env = os.environ.copy()
    env["CENTRAL_IFACE"] = name
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"interface {name} has no IPv4 address") from exc
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    addr = lines[-1] if lines else ""
    if proc.returncode != 0 or not _ip_literal(addr):
        raise ValueError(f"interface {name} has no IPv4 address")
    return addr


def resolve_interface(name: str, *, allow_any: bool | None = None) -> str:
    """Return the address to bind.

    ``0.0.0.0`` is refused unless ``allow_any`` or ``CENTRAL_BIND_ANY=1``.
    A Niagara PC should listen on the named LAN interface, not on every NIC.
    """
    text = (name or "").strip()
    if not text:
        raise ValueError("interface is required")
    any_ok = allow_any if allow_any is not None else os.environ.get("CENTRAL_BIND_ANY", "").strip() == "1"
    if text in {"0.0.0.0", "::"}:
        if not any_ok:
            raise ValueError(
                "refusing to bind every interface. Set interface to the LAN name "
                "(for example Ethernet) or to one address"
            )
        return text
    if _ip_literal(text):
        return text
    if sys.platform == "win32":
        return _windows_ipv4(text)
    return _linux_ipv4(text)
