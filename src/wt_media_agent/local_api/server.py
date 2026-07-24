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
from wt_media_agent.proxy_check import check_proxy_connectivity
from wt_media_agent.runtimes.environment import RuntimeEnvironmentCollector

logger = logging.getLogger(__name__)


class ProfileScanner(Protocol):
    def scan_profiles(self) -> ProfileSnapshot: ...
    def create_profile(self, config: dict[str, object]) -> str: ...
    def open_profile(self, profile_id: str) -> None: ...
    def close_profile(self, profile_id: str) -> None: ...
    def update_profile(self, profile_id: str, config: dict[str, object]) -> None: ...
    def delete_profile(self, profile_id: str) -> None: ...
    def read_cookies(self, profile_id: str) -> list[dict[str, object]]: ...


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
        environment = RuntimeEnvironmentCollector(bitbrowser=self.bitbrowser).collect().to_dict()
        base.update(environment)
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

    def profile_create_response(self, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        try:
            profile_id = self.bitbrowser.create_profile(body)
            return 201, {"data": {"id": profile_id}}
        except BitBrowserResponseError as e:
            return 502, {"error": {"code": "bitbrowser_response_error", "message": str(e)}}

    def profile_open_response(self, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        profile_id = body.get("id", "")
        if not profile_id:
            return 400, {"error": {"code": "profile_id_required"}}
        try:
            self.bitbrowser.open_profile(profile_id)
            return 200, {"data": {"status": "opened"}}
        except BitBrowserResponseError as e:
            return 502, {"error": {"code": "bitbrowser_response_error", "message": str(e)}}

    def profile_close_response(self, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        profile_id = body.get("id", "")
        if not profile_id:
            return 400, {"error": {"code": "profile_id_required"}}
        try:
            self.bitbrowser.close_profile(profile_id)
            return 200, {"data": {"status": "closed"}}
        except BitBrowserResponseError as e:
            return 502, {"error": {"code": "bitbrowser_response_error", "message": str(e)}}

    def profile_update_response(self, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        profile_id = body.get("id", "")
        if not profile_id:
            return 400, {"error": {"code": "profile_id_required"}}
        try:
            self.bitbrowser.update_profile(profile_id, body)
            return 200, {"data": {"status": "updated"}}
        except BitBrowserResponseError as e:
            return 502, {"error": {"code": "bitbrowser_response_error", "message": str(e)}}

    def profile_delete_response(self, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        profile_id = body.get("id", "")
        if not profile_id:
            return 400, {"error": {"code": "profile_id_required"}}
        try:
            self.bitbrowser.delete_profile(profile_id)
            return 200, {"data": {"status": "deleted"}}
        except BitBrowserResponseError as e:
            return 502, {"error": {"code": "bitbrowser_response_error", "message": str(e)}}

    def proxy_check_response(self, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        """Run the short TCP check inline; browser interaction is not involved."""
        host = str(body.get("host", "")).strip()
        try:
            port = int(body.get("port", 0))
        except (TypeError, ValueError):
            port = 0
        if not host or not 1 <= port <= 65535:
            return 400, {"error": {"code": "proxy_input_invalid"}}
        result = check_proxy_connectivity(host, port)
        data: dict[str, object] = {"connectivity": result}
        proxy_id = str(body.get("proxy_id", "")).strip()
        if proxy_id:
            data["proxy_id"] = proxy_id
        return 200, {"data": data}

    def account_check_response(self, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        """Synchronously inspect one BitBrowser profile and return safe account identity facts."""
        profile_id = str(body.get("profile_id", "")).strip()
        platform = str(body.get("platform", "")).strip().lower()
        expected_id = str(body.get("expected_platform_account_id", "")).strip()
        if not profile_id or platform not in {"bilibili", "baijiahao", "douyin"}:
            return 400, {"error": {"code": "account_check_input_invalid"}}
        try:
            self.bitbrowser.open_profile(profile_id)
            cookies = self.bitbrowser.read_cookies(profile_id)
        except BitBrowserIdentityError:
            return 409, {"error": {"code": "bitbrowser_identity_unverifiable"}}
        except BitBrowserResponseError as e:
            return 502, {"error": {"code": "bitbrowser_response_error", "message": str(e)}}

        result = _identify_platform_account(platform, cookies)
        if expected_id and result.get("platform_account_id") and result["platform_account_id"] != expected_id:
            result["login_status"] = "account_mismatch"
            result["message"] = "当前窗口登录账号与媒体账号台账不一致"
        return 200, {"data": result}


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
            elif self.path == "/api/v1/bit-browser/profile-create":
                status, payload = api.profile_create_response(self._read_body())
                self._write_json(status, payload)
            elif self.path == "/api/v1/bit-browser/profile-open":
                status, payload = api.profile_open_response(self._read_body())
                self._write_json(status, payload)
            elif self.path == "/api/v1/bit-browser/profile-close":
                status, payload = api.profile_close_response(self._read_body())
                self._write_json(status, payload)
            elif self.path == "/api/v1/bit-browser/profile-update":
                status, payload = api.profile_update_response(self._read_body())
                self._write_json(status, payload)
            elif self.path == "/api/v1/bit-browser/profile-delete":
                status, payload = api.profile_delete_response(self._read_body())
                self._write_json(status, payload)
            elif self.path == "/api/v1/proxy-check":
                status, payload = api.proxy_check_response(self._read_body())
                self._write_json(status, payload)
            elif self.path == "/api/v1/account-check":
                status, payload = api.account_check_response(self._read_body())
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
            self._cors_headers()
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
            node_id = str(payload.get("node_id", "local-agent-dev"))
            api.state.node_id = node_id
            self._write_json(200, {
                "node_id": node_id,
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

        def _read_body(self) -> dict[str, object]:
            try:
                length = int(self.headers.get("content-length", 0))
                if not length:
                    return {}
                body = self.rfile.read(length)
                return json.loads(body) if body else {}
            except (ValueError, json.JSONDecodeError):
                return {}

        def _cors_headers(self) -> None:
            self.send_header("access-control-allow-origin", "*")
            self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
            self.send_header("access-control-allow-headers", "authorization, content-type")

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self._cors_headers()
            self.end_headers()

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def _write_json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self._cors_headers()
            self.end_headers()
            self.wfile.write(body)

    return AgentHandler


def _identify_platform_account(platform: str, cookies: list[dict[str, object]]) -> dict[str, object]:
    """Extract only safe platform identity facts. Cookie values never leave this function except known public IDs."""
    cookie_by_name: dict[str, str] = {}
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name", "")).strip()
        value = str(cookie.get("value", "")).strip()
        if name and value:
            cookie_by_name[name] = value

    if not cookie_by_name:
        return {
            "platform_account_id": "",
            "name": "",
            "avatar_url": "",
            "login_status": "not_logged_in",
            "message": "当前窗口未读取到登录Cookie",
        }

    if platform == "bilibili":
        uid = cookie_by_name.get("DedeUserID", "").strip()
        if uid:
            return {
                "platform_account_id": uid,
                "name": "",
                "avatar_url": "",
                "login_status": "normal",
                "message": "已读取到哔哩哔哩账号UID",
            }

    return {
        "platform_account_id": "",
        "name": "",
        "avatar_url": "",
        "login_status": "environment_error",
        "message": "当前平台暂未读取到可确认的平台账号UID",
    }


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
