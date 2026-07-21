"""Synchronous proxy connectivity checks shared by local API and task executor."""

from __future__ import annotations

import socket


def check_proxy_connectivity(host: str, port: int, timeout: float = 5.0) -> str:
    """Return a stable, UI-safe connectivity result without exposing credentials."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "reachable"
    except OSError as exc:
        return f"unreachable: {type(exc).__name__}"
