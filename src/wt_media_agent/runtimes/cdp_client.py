"""Minimal CDP (Chrome DevTools Protocol) client over raw WebSocket.

Used to read a BitBrowser profile's live cookies and evaluate a fetch inside the
page (some platforms like Bilibili reject server-side API calls via risk control,
but in-page fetches with the browser's cookies/headers are accepted).
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
import urllib.request
from typing import Any, Callable


class CDPError(RuntimeError):
    pass


def _ws_connect(host: str, port: int, path: str, timeout: float = 15.0) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(request.encode())
    response = b""
    while b"\r\n\r\n" not in response:
        response += sock.recv(4096)
    if b"101" not in response.split(b"\r\n")[0]:
        raise CDPError("WebSocket upgrade failed")
    return sock


def _ws_send(sock: socket.socket, message: str) -> None:
    payload = message.encode()
    mask = os.urandom(4)
    length = len(payload)
    if length < 126:
        header = bytes([0x81, 0x80 | length])
    elif length < 65536:
        header = bytes([0x81, 0x80 | 126]) + struct.pack(">H", length)
    else:
        header = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", length)
    header += mask
    sock.sendall(header + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def _ws_recv(sock: socket.socket, timeout: float = 30.0) -> Any:
    sock.settimeout(timeout)
    data = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
        try:
            idx = data.find(b"{")
            return json.loads(data[idx:].decode(errors="replace"))
        except (ValueError, json.JSONDecodeError):
            if len(data) > 20_000_000:
                raise CDPError("CDP response too large")
    raise CDPError("CDP connection closed")


class CDPPage:
    """A connection to one CDP page target."""

    def __init__(self, ws_url: str) -> None:
        rest = ws_url.replace("ws://", "")
        host_port, _, path = rest.partition("/")
        host, _, port = host_port.rpartition(":")
        self._sock = _ws_connect(host, int(port), "/" + path)
        self._counter = 0

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._counter += 1
        request_id = self._counter
        _ws_send(self._sock, json.dumps({"id": request_id, "method": method, "params": params or {}}))
        while True:
            message = _ws_recv(self._sock)
            if message.get("id") == request_id:
                if "error" in message:
                    raise CDPError(f"CDP {method} error: {message['error']}")
                return message.get("result", {})

    def evaluate(self, expression: str) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        value = result.get("result", {})
        if value.get("subtype") == "error":
            raise CDPError(f"JS error: {value.get('description')}")
        return value.get("value")

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def _find_page_ws(http_endpoint: str) -> str:
    """Return the WS URL of the first 'page' target on a DevTools http endpoint."""
    with urllib.request.urlopen(f"http://{http_endpoint}/json", timeout=10) as response:
        targets = json.loads(response.read().decode())
    for target in targets:
        if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
            return target["webSocketDebuggerUrl"]
    raise CDPError("no page target found")


def read_live_cookies(http_endpoint: str) -> list[dict[str, Any]]:
    """Read live cookies from the profile's browser via Network.getAllCookies."""
    page = CDPPage(_find_page_ws(http_endpoint))
    try:
        result = page.call("Network.getAllCookies", {})
        cookies = result.get("cookies", [])
        return cookies if isinstance(cookies, list) else []
    finally:
        page.close()


def eval_fetch_json(http_endpoint: str, url: str, timeout_ms: int = 10000) -> dict[str, Any]:
    """Evaluate an in-page fetch to url and return the parsed JSON body."""
    expression = (
        f"(async () => {{"
        f"  const r = await fetch({json.dumps(url)}, {{ credentials: 'include' }});"
        f"  const t = await r.text();"
        f"  return JSON.stringify({{ status: r.status, body: t }});"
        f"}})()"
    )
    page = CDPPage(_find_page_ws(http_endpoint))
    try:
        raw = page.evaluate(expression)
        if not isinstance(raw, str):
            raise CDPError("in-page fetch returned no value")
        parsed = json.loads(raw)
        body_text = parsed.get("body", "")
        return {"status": parsed.get("status"), "json": _safe_json(body_text)}
    finally:
        page.close()


def _safe_json(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}
