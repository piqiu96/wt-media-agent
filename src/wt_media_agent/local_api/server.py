"""Local Agent control API scaffold."""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Protocol

from wt_media_agent.local_api.state import LocalAgentState
from wt_media_agent.runtimes.bitbrowser import (
    BitBrowserClient,
    BitBrowserIdentityError,
    BitBrowserResponseError,
    ProfileSnapshot,
)


class ProfileScanner(Protocol):
    def scan_profiles(self) -> ProfileSnapshot: ...


class LocalApiServer:
    """Exposes the M0 local process health surface."""

    def __init__(
        self,
        state: Optional[LocalAgentState] = None,
        bitbrowser: ProfileScanner | None = None,
    ) -> None:
        self.state = state or LocalAgentState()
        self.bitbrowser = bitbrowser or BitBrowserClient(
            os.getenv("WT_MEDIA_BITBROWSER_API_URL", "http://127.0.0.1:54345"),
            timeout=float(os.getenv("WT_MEDIA_BITBROWSER_TIMEOUT_SECONDS", "5")),
        )

    def health(self) -> dict[str, str]:
        return {"status": "ok", "service": "wt-media-agent", "mode": "m1"}

    def status(self) -> dict[str, object]:
        return self.state.snapshot()

    def event_stream_snapshot(self) -> str:
        return self.state.sse_snapshot()

    def profile_scan_response(self) -> tuple[int, dict[str, object]]:
        try:
            return 200, self.bitbrowser.scan_profiles().to_dict()
        except BitBrowserIdentityError:
            return 409, {"error": {"code": "bitbrowser_identity_unverifiable"}}
        except BitBrowserResponseError:
            return 502, {"error": {"code": "bitbrowser_response_error"}}


def make_handler(api: LocalApiServer) -> type[BaseHTTPRequestHandler]:
    class HealthHandler(BaseHTTPRequestHandler):
        server_version = "WTMediaLocalAgentM0/0.1"

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._write_json(200, api.health())
                return
            if self.path == "/api/v1/status":
                self._write_json(200, api.status())
                return
            if self.path == "/api/v1/events":
                self._write_sse(200, api.event_stream_snapshot())
                return
            self._write_json(404, {"error": "not_found"})

        def do_POST(self) -> None:
            if self.path == "/api/v1/bit-browser/profile-scans":
                status, payload = api.profile_scan_response()
                self._write_json(status, payload)
                return
            self._write_json(404, {"error": "not_found"})

        def log_message(self, format: str, *args: object) -> None:
            return

        def _write_json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _write_sse(self, status: int, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return HealthHandler


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    api = LocalApiServer()
    httpd = ThreadingHTTPServer((host, port), make_handler(api))
    print(f"wt-media-agent local health listening on {host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("wt-media-agent local health stopped", flush=True)
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args(argv)
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
