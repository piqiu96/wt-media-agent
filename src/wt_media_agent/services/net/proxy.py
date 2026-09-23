"""Proxy connectivity probing and proxy-address parsing.

Shared by the local API control plane and the proxy task executors. Kept as one
module rather than three: the three functions form a single cohesive concern
(proxy reachability and address parsing) and splitting them would add
abstractions without changing any boundary.
"""

from __future__ import annotations

import socket
from urllib import parse as urlparse


def check_proxy_connectivity(host: str, port: int, timeout: float = 5.0) -> str:
    """Return a stable, UI-safe connectivity result without exposing credentials."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "reachable"
    except OSError as exc:
        return f"unreachable: {type(exc).__name__}"


def parse_first_proxy_address(raw: str, default_protocol: str) -> dict[str, object]:
    for line in raw.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        parsed = parse_proxy_address(candidate, default_protocol)
        if parsed is not None:
            return parsed
    raise ValueError("no supported proxy address")


def parse_proxy_address(raw: str, default_protocol: str) -> dict[str, object] | None:
    if "://" in raw:
        value = urlparse.urlsplit(raw)
        if value.scheme not in {"http", "https", "socks5"} or not value.hostname or value.port is None:
            return None
        return {
            "proxy_protocol": value.scheme,
            "host": value.hostname,
            "port": value.port,
            "username": value.username or "",
            "password": value.password or "",
        }
    parts = raw.split(":", 3)
    if len(parts) < 2 or not parts[0]:
        return None
    try:
        port = int(parts[1])
    except ValueError:
        return None
    if not 1 <= port <= 65535:
        return None
    result: dict[str, object] = {"proxy_protocol": default_protocol, "host": parts[0], "port": port, "username": "", "password": ""}
    if len(parts) == 4:
        result["username"], result["password"] = parts[2], parts[3]
    elif len(parts) != 2:
        return None
    return result
