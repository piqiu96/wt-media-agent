"""Local Agent HTTP/SSE API with task status, events, and node binding."""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Protocol

from wt_media_agent.local_api.state import LocalAgentState
from wt_media_agent.storage.checkpoint_store import CheckpointStore
from wt_media_agent.runtimes.bitbrowser import (
    BitBrowserClient,
    BitBrowserIdentityError,
    BitBrowserResponseError,
    ProfileSnapshot,
)

logger = logging.getLogger(__name__)


class ProfileScanner(Protocol):
    def scan_profiles(self) -> ProfileSnapshot: ...


class LocalApiServer:
    """Exposes the Local Agent health, status, events, and task control surface."""

    def __init__(
        self,
        state: Optional[LocalAgentState] = None,
        bitbrowser: ProfileScanner | None = None,
        checkpoint_store: Optional[CheckpointStore] = None,
        auth_token: str = "",
    ) -> None:
        self.state = state or LocalAgentState()
        self.store = checkpoint_store
        self.auth_token = auth_token or ""
        self._event_queue: queue.Queue[dict[str, object]] = queue.Queue()
        self.bitbrowser = bitbrowser or BitBrowserClient(
            os.getenv("WT_MEDIA_BITBROWSER_API_URL", "http://127.0.0.1:54345"),
            timeout=float(os.getenv("WT_MEDIA_BITBROWSER_TIMEOUT_SECONDS", "5")),
        )

    def _check_auth(self, headers: dict[str, str]) -> bool:
        if not self.auth_token:
            return True
        auth = headers.get("authorization", "")
        return auth == f"Bearer {self.auth_token}"

    def health(self) -> dict[str, str]:
        return {"status": "ok", "service": "wt-media-agent", "mode": "m1"}

    def status(self) -> dict[str, object]:
        base = self.state.snapshot()
        if self.store:
            incomplete = self.store.get_incomplete_checkpoints()
            if incomplete:
                cp = incomplete[0]
                base["current_task_id"] = cp.task_id
                base["current_task_progress"] = cp.progress
                base["current_task_status"] = cp.checkpoint_status
            base["pending_result_count"] = len(self.store.get_undelivered_results())
        return base

    def push_event(self, event_type: str, data: dict[str, object]) -> None:
        self._event_queue.put({"event": event_type, "data": data})

    def event_stream_snapshot(self) -> str:
        return self.state.sse_snapshot()

    def consume_event(self) -> Optional[dict[str, object]]:
        try:
            return self._event_queue.get_nowait()
        except queue.Empty:
            return None

    def profile_scan_response(self) -> tuple[int, dict[str, object]]:
        try:
            return 200, self.bitbrowser.scan_profiles().to_dict()
        except BitBrowserIdentityError:
            return 409, {"error": {"code": "bitbrowser_identity_unverifiable"}}
        except BitBrowserResponseError:
            return 502, {"error": {"code": "bitbrowser_response_error"}}


def make_handler(api: LocalApiServer) -> type[BaseHTTPRequestHandler]:
    class AgentHandler(BaseHTTPRequestHandler):
        server_version = "WTMediaAgentM1/0.1"

        def _check_auth(self) -> bool:
            if not api.auth_token:
                return True
            auth = self.headers.get("authorization", "")
            return auth == f"Bearer {api.auth_token}"

        def do_GET(self) -> None:
            if not self._check_auth():
                self._write_json(401, {"error": "unauthorized"})
                return
            if self.path == "/healthz":
                self._write_json(200, api.health())
            elif self.path == "/api/v1/status":
                self._write_json(200, {"data": api.status()})
            elif self.path == "/api/v1/events":
                self._handle_sse_stream()
            else:
                self._write_json(404, {"error": "not_found"})

        def do_POST(self) -> None:
            if not self._check_auth():
                self._write_json(401, {"error": "unauthorized"})
                return
            if self.path == "/api/v1/bit-browser/profile-scans":
                status, payload = api.profile_scan_response()
                self._write_json(status, payload)
            elif self.path == "/api/v1/bind":
                self._handle_bind()
            else:
                self._write_json(404, {"error": "not_found"})

        def _handle_sse_stream(self) -> None:
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.send_header("connection", "keep-alive")
            self.end_headers()

            # Send initial status snapshot.
            snapshot = json.dumps(api.status(), separators=(",", ":"))
            self._sse_write("status", snapshot)

            # Poll for events until client disconnects.
            try:
                while True:
                    event = api.consume_event()
                    if event:
                        body = json.dumps(event["data"], separators=(",", ":"))
                        self._sse_write(str(event.get("event", "message")), body)
                    else:
                        # Heartbeat every 15s.
                        self._sse_write("heartbeat", "")
                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError):
                logger.debug("SSE client disconnected")
            finally:
                try:
                    self.wfile.flush()
                except Exception:
                    pass

        def _handle_bind(self) -> None:
            try:
                length = int(self.headers.get("content-length", 0))
                body = self.rfile.read(length) if length else b"{}"
                payload = json.loads(body)
            except (ValueError, json.JSONDecodeError):
                self._write_json(400, {"error": "invalid_json"})
                return

            binding_token = payload.get("binding_token", "")
            if not binding_token or not isinstance(binding_token, str):
                self._write_json(400, {"error": "binding_token_required"})
                return

            # Generate a session token for this binding.
            session_token = secrets.token_hex(32)
            self._write_json(200, {
                "node_id": payload.get("node_id", "local-agent-dev"),
                "session_token": session_token,
                "status": "bound",
            })

        def _sse_write(self, event: str, data: str) -> None:
            try:
                if event:
                    self.wfile.write(f"event: {event}\n".encode())
                if data:
                    self.wfile.write(f"data: {data}\n".encode())
                self.wfile.write(b"\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                raise

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def _write_json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return AgentHandler


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    checkpoint_store: Optional[CheckpointStore] = None,
    auth_token: str = "",
) -> None:
    api = LocalApiServer(checkpoint_store=checkpoint_store, auth_token=auth_token)
    httpd = ThreadingHTTPServer((host, port), make_handler(api))
    logger.info("wt-media-agent local API listening on %s:%s", host, port)
    print(f"wt-media-agent local API listening on {host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("wt-media-agent local API stopped", flush=True)
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--auth-token", default="")
    args = parser.parse_args(argv)
    serve(args.host, args.port, auth_token=args.auth_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
