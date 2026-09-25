"""Local Agent HTTP/SSE API with task status, events, and node binding."""

from __future__ import annotations

import argparse
import json
import logging
import queue
import secrets
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Protocol
from urllib import parse as urlparse
from urllib import request as urlrequest

from wt_media_agent.bootstrap.app import build_components
from wt_media_agent.local_api.health import CloudReachability, health_report
from wt_media_agent.local_api.reporting import (
    _build_account_check_items,
    _duration_ms,
    _safe_groups,
)
from wt_media_agent.local_api.state import LocalAgentState
from wt_media_agent.storage.checkpoint_store import CheckpointStore
from wt_media_agent.clients.bitbrowser import (
    BitBrowserIdentityError,
    BitBrowserResponseError,
    ProfileSnapshot,
)
from wt_media_agent.clients import platform_identity
from wt_media_agent.clients.baijiahao import identity as baijiahao_identity
from wt_media_agent.clients.bilibili import identity as bilibili_identity
from wt_media_agent.services.browser import cookies as browser_cookies
from wt_media_agent.services.net.proxy import (
    check_proxy_connectivity,
    parse_first_proxy_address,
)
from wt_media_agent.runtime.environment import RuntimeEnvironmentCollector
from wt_media_agent.runtime.logging import begin_operation, end_operation

#: Spelled out rather than `__name__`. `scripts/verify-health.sh` and
#: `bin/control.sh` both run this module with `-m`, where `__name__`
#: is `__main__` -- so the logger was named `__main__` and every HTTP-side
#: record lost the component it came from (T-07 measured it, T-09 fixed it).
#: The literal is what `__name__` already produced on the import path, so
#: nothing moved for the entry points that import the module.
LOGGER_NAME = "wt_media_agent.local_api.server"

logger = logging.getLogger(LOGGER_NAME)


class ProfileScanner(Protocol):
    def scan_profiles(self) -> ProfileSnapshot: ...
    def create_profile(self, config: dict[str, object]) -> str: ...
    def open_profile(self, profile_id: str) -> None: ...
    def close_profile(self, profile_id: str) -> None: ...
    def update_profile(self, profile_id: str, config: dict[str, object]) -> None: ...
    def delete_profile(self, profile_id: str) -> None: ...
    def group_list(self) -> list[dict[str, object]]: ...
    def read_cookies(self, profile_id: str) -> list[dict[str, object]]: ...


class LocalApiServer:
    """Exposes the Local Agent health, status, events, and task control surface."""

    def __init__(
        self,
        state: LocalAgentState,
        *,
        bitbrowser: ProfileScanner,
        checkpoint_store: Optional[CheckpointStore] = None,
        auth_token: str = "",
        cloud_base_url: str = "",
    ) -> None:
        # Mandatory, like `bitbrowser` below and for the same reason (T-09):
        # `state or LocalAgentState()` gave a caller that forgot the argument a
        # fresh, plausible-looking state -- agent_id "local-agent-dev", status
        # "idle" -- so `/api/v1/status` would have described an agent that was
        # not the one running, with nothing to show that it had.
        self.state = state
        self.store = checkpoint_store
        self.auth_token = auth_token or ""
        # The configured Cloud endpoint, handed in rather than read here (the
        # configuration layer is the only reader of `config/` and the
        # environment). Empty means this process has no Cloud to report on,
        # which `/api/v1/health` says as `unknown`.
        self.cloud = CloudReachability(base_url=cloud_base_url)
        self._event_queue: queue.Queue[dict[str, object]] = queue.Queue()
        # Mandatory, and with no default: this class used to build a client
        # from the environment when none was passed, which meant every route
        # here could reach BitBrowser without anyone deciding it should. The
        # client comes from `bootstrap` (ADR-0016 §2).
        self.bitbrowser = bitbrowser

    def _check_auth(self, headers: dict[str, str]) -> bool:
        if not self.auth_token:
            return True
        auth = headers.get("authorization", "")
        return auth == f"Bearer {self.auth_token}"

    def health(self) -> dict[str, str]:
        """`/healthz`. Frozen: three keys, and it stays that way.

        Desktop's `http/local_agent.rs` parses exactly this body, and the
        architecture baseline §5.6 lists the path as a contract that must not be
        extended. The aggregate belongs to `health_report` below.
        """
        return {"status": "ok", "service": "wt-media-agent", "mode": "m1"}

    def health_report_response(self) -> dict[str, object]:
        """`/api/v1/health`: the aggregate, and never a raised dependency error.

        `local_api/health.py` owns the probes and the degradation vocabulary;
        this is the surface that exposes them, with the same arguments the
        process was assembled with.
        """
        return health_report(
            self.state, self.bitbrowser, self.store, self.cloud
        ).to_dict()

    def status(self) -> dict[str, object]:
        started_at = time.monotonic()
        logger.info("local_api.status.start")
        base = self.state.snapshot()
        try:
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
            logger.info(
                "local_api.status.success duration_ms=%d bitbrowser_status=%s main_user_id=%s profile_count=%d",
                _duration_ms(started_at),
                base.get("bitbrowser_status", ""),
                base.get("main_user_id", ""),
                len(base.get("bit_profile_ids", []) or []),
            )
            return base
        except Exception:
            logger.exception("local_api.status.failure duration_ms=%d", _duration_ms(started_at))
            raise

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
        started_at = time.monotonic()
        profile_name = str(body.get("name", "")).strip()
        group_id = str(body.get("groupId", "") or body.get("group_id", "")).strip()
        logger.info("local_api.profile_create.start name=%s group_id=%s", profile_name, group_id)
        try:
            profile_id = self.bitbrowser.create_profile(body)
            logger.info(
                "local_api.profile_create.success profile_id=%s duration_ms=%d",
                profile_id,
                _duration_ms(started_at),
            )
            return 201, {"data": {"id": profile_id}}
        except BitBrowserResponseError as e:
            logger.warning(
                "local_api.profile_create.failure name=%s group_id=%s duration_ms=%d error=%s",
                profile_name,
                group_id,
                _duration_ms(started_at),
                e,
            )
            return 502, {"error": {"code": "bitbrowser_response_error", "message": str(e)}}

    def profile_groups_response(self) -> tuple[int, dict[str, object]]:
        try:
            return 200, {"data": {"groups": _safe_groups(self.bitbrowser.group_list())}}
        except BitBrowserResponseError as e:
            return 502, {"error": {"code": "bitbrowser_response_error", "message": str(e)}}

    def profile_open_response(self, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        profile_id = body.get("id", "")
        if not profile_id:
            logger.warning("local_api.profile_open.reject reason=profile_id_required")
            return 400, {"error": {"code": "profile_id_required"}}
        started_at = time.monotonic()
        logger.info("local_api.profile_open.start profile_id=%s", profile_id)
        try:
            self.bitbrowser.open_profile(profile_id)
            logger.info(
                "local_api.profile_open.success profile_id=%s duration_ms=%d",
                profile_id,
                _duration_ms(started_at),
            )
            return 200, {"data": {"status": "opened"}}
        except BitBrowserResponseError as e:
            logger.warning(
                "local_api.profile_open.failure profile_id=%s duration_ms=%d error=%s",
                profile_id,
                _duration_ms(started_at),
                e,
            )
            return 502, {"error": {"code": "bitbrowser_response_error", "message": str(e)}}

    def profile_close_response(self, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        profile_id = body.get("id", "")
        if not profile_id:
            logger.warning("local_api.profile_close.reject reason=profile_id_required")
            return 400, {"error": {"code": "profile_id_required"}}
        started_at = time.monotonic()
        logger.info("local_api.profile_close.start profile_id=%s", profile_id)
        try:
            self.bitbrowser.close_profile(profile_id)
            logger.info(
                "local_api.profile_close.success profile_id=%s duration_ms=%d",
                profile_id,
                _duration_ms(started_at),
            )
            return 200, {"data": {"status": "closed"}}
        except BitBrowserResponseError as e:
            logger.warning(
                "local_api.profile_close.failure profile_id=%s duration_ms=%d error=%s",
                profile_id,
                _duration_ms(started_at),
                e,
            )
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

    def proxy_extract_response(self, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        """Fetch and parse one provider response without touching BitBrowser."""
        extract_url = str(body.get("extract_url", "")).strip()
        protocol = str(body.get("proxy_protocol", "http")).strip().lower() or "http"
        parsed_url = urlparse.urlsplit(extract_url)
        if protocol not in {"http", "https", "socks5"} or parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            return 400, {"error": {"code": "proxy_extract_input_invalid"}}
        try:
            request = urlrequest.Request(extract_url, headers={"Accept": "text/plain"})
            with urlrequest.urlopen(request, timeout=7) as response:
                raw = response.read(64 * 1024).decode("utf-8", errors="replace")
            address = parse_first_proxy_address(raw, protocol)
        except ValueError:
            return 400, {"error": {"code": "proxy_extract_response_invalid"}}
        except (OSError, TimeoutError):
            return 502, {"error": {"code": "proxy_extract_request_failed"}}
        return 200, {"data": address}

    def proxy_mutation_response(self, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        """Write or remove one Profile proxy, then return verified secret-free facts."""
        profile_id = str(body.get("profile_id", "")).strip()
        operation = str(body.get("operation", "assign")).strip().lower() or "assign"
        if operation == "unbind":
            if not profile_id:
                return 400, {"error": {"code": "proxy_mutation_input_invalid"}}
            try:
                self.bitbrowser.update_profile(profile_id, {
                    "proxyType": "noproxy",
                    "proxyMethod": 2,
                    # BitBrowser keeps the old address and credentials unless the
                    # clear values are sent explicitly. They must disappear before
                    # Cloud can record the formal unbind.
                    "host": "",
                    "port": 0,
                    "proxyUserName": "",
                    "proxyPassword": "",
                })
                snapshot = self.bitbrowser.scan_profiles()
            except BitBrowserIdentityError:
                return 409, {"error": {"code": "bitbrowser_identity_unverifiable"}}
            except BitBrowserResponseError:
                return 502, {"error": {"code": "bitbrowser_response_error"}}
            found = next((item for item in snapshot.profiles if item.bit_profile_id == profile_id), None)
            if found is None or found.proxy_type.lower() != "noproxy" or found.proxy_host or found.proxy_port:
                return 409, {"error": {"code": "proxy_mutation_readback_mismatch"}}
            return 200, {"data": {"operation": "unbind", "profile_id": profile_id, "readback": True}}
        if operation != "assign":
            return 400, {"error": {"code": "proxy_mutation_input_invalid"}}
        protocol = str(body.get("proxy_protocol", "")).strip().lower()
        host = str(body.get("host", "")).strip()
        try:
            port = int(body.get("port", 0))
        except (TypeError, ValueError):
            port = 0
        if not profile_id or protocol not in {"http", "https", "socks5"} or not host or not 1 <= port <= 65535:
            return 400, {"error": {"code": "proxy_mutation_input_invalid"}}
        try:
            self.bitbrowser.update_profile(profile_id, {
                "proxyType": protocol,
                "host": host,
                "port": port,
                "proxyUserName": str(body.get("username", "")),
                "proxyPassword": str(body.get("password", "")),
            })
            snapshot = self.bitbrowser.scan_profiles()
        except BitBrowserIdentityError:
            return 409, {"error": {"code": "bitbrowser_identity_unverifiable"}}
        except BitBrowserResponseError:
            return 502, {"error": {"code": "bitbrowser_response_error"}}
        found = next((item for item in snapshot.profiles if item.bit_profile_id == profile_id), None)
        if found is None or found.proxy_type.lower() != protocol or found.proxy_host != host or found.proxy_port != port:
            return 409, {"error": {"code": "proxy_mutation_readback_mismatch"}}
        return 200, {"data": {
            "profile_id": profile_id,
            "proxy_protocol": protocol,
            "host": host,
            "port": port,
            "readback": True,
        }}

    def account_check_response(self, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        """Synchronously inspect one BitBrowser profile and return safe account identity facts."""
        profile_id = str(body.get("profile_id", "")).strip()
        platform = str(body.get("platform", "")).strip().lower()
        expected_id = str(body.get("expected_platform_account_id", "")).strip()
        if not profile_id or platform not in {"bilibili", "baijiahao", "douyin"}:
            return 400, {"error": {"code": "account_check_input_invalid"}}
        devtools = ""
        try:
            devtools = self.bitbrowser.open_profile_with_devtools(profile_id)
            cookies = browser_cookies.read_account_cookies(self.bitbrowser, profile_id, devtools)
        except BitBrowserIdentityError:
            return 409, {"error": {"code": "bitbrowser_identity_unverifiable"}}
        except BitBrowserResponseError as e:
            return 502, {"error": {"code": "bitbrowser_response_error", "message": str(e)}}

        nav_data = None
        if platform == "bilibili" and devtools:
            nav_data = bilibili_identity.fetch_nav_via_cdp(devtools)

        if platform == "baijiahao":
            result = baijiahao_identity.identify_baijiahao(cookies)
        elif platform == "bilibili":
            result = bilibili_identity.identify_bilibili(cookies, nav_data)
        else:
            result = platform_identity.identify_platform_account(platform, cookies)
        if expected_id and result.get("platform_account_id") and result["platform_account_id"] != expected_id:
            result["login_status"] = "account_mismatch"
            result["message"] = "当前窗口登录账号与媒体账号台账不一致"
        result["check_items"] = _build_account_check_items(result, bool(expected_id))
        return 200, {"data": result}

    def cookie_read_response(self, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        """Synchronously read cookies from a BitBrowser profile.

        Returns the real cookie list so the caller can refresh active_cookie.
        Cookie data is sensitive; callers must follow secret_policy.
        """
        profile_id = str(body.get("profile_id", "")).strip()
        if not profile_id:
            return 400, {"error": {"code": "cookie_read_input_invalid"}}
        try:
            self.bitbrowser.open_profile(profile_id)
            cookies = self.bitbrowser.read_cookies(profile_id)
        except BitBrowserIdentityError:
            return 409, {"error": {"code": "bitbrowser_identity_unverifiable"}}
        except BitBrowserResponseError as e:
            return 502, {"error": {"code": "bitbrowser_response_error", "message": str(e)}}
        return 200, {"data": {"cookies": cookies}}


def make_handler(api: LocalApiServer) -> type[BaseHTTPRequestHandler]:
    class AgentHandler(BaseHTTPRequestHandler):
        server_version = "WTMediaAgentM1/0.1"

        def handle_one_request(self) -> None:
            """One request, with one id of its own (T-21).

            Every request begins here, so the id is set here rather than in each
            `do_*`: a 401, a 404, an OPTIONS preflight and a malformed request
            line all pass through this method, so all of them are inside an id by
            construction -- and a route added later cannot forget to set one. The
            reset is in a `finally` because the id must not outlive the request it
            names, including when the request ends by raising.

            The generator lives here, not in `runtime.logging`: that module owns
            where the field goes in a line, while "what counts as one request" is
            this module's question. `secrets` is already imported for the binding
            token, so this adds no dependency.

            One consequence, deliberate and registered: an SSE stream is a single
            `handle_one_request` call, so every record a stream writes shares its
            id rather than getting one per event.
            """
            token = begin_operation(secrets.token_hex(8))
            try:
                super().handle_one_request()
            finally:
                end_operation(token)

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
            elif self.path == "/api/v1/health":
                self._write_json(200, api.health_report_response())
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
            elif self.path == "/api/v1/bit-browser/profile-groups":
                status, payload = api.profile_groups_response()
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
            elif self.path == "/api/v1/proxy-extract":
                status, payload = api.proxy_extract_response(self._read_body())
                self._write_json(status, payload)
            elif self.path == "/api/v1/proxy-mutation":
                status, payload = api.proxy_mutation_response(self._read_body())
                self._write_json(status, payload)
            elif self.path == "/api/v1/account-check":
                status, payload = api.account_check_response(self._read_body())
                self._write_json(status, payload)
            elif self.path == "/api/v1/cookie-read":
                status, payload = api.cookie_read_response(self._read_body())
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


def _answer_sigterm(httpd: ThreadingHTTPServer) -> object:
    """Route SIGTERM to the ordinary exit path, and report the displaced handler.

    Desktop stops this process by **asking**, not by killing
    (`wt-media-desktop/src-tauri/src/sidecar/mod.rs`, CHG-059 T-03): it sends
    SIGTERM, waits a grace window, and only then sends SIGKILL. SIGTERM's default
    disposition is an immediate termination that runs no Python at all, so
    without this the ask *is* a kill that costs a few seconds — the in-flight
    work is lost just as surely, and Desktop's two exit readings become
    indistinguishable.

    What is deliberately **not** written here: any deadline. `shutdown()` stops
    the accept loop, `server_close()` joins the request threads (see the flag
    `serve` sets for that), and the deadline belongs to the side that can
    enforce it — Desktop waits a grace window and then sends SIGKILL. A second
    SIGTERM during that wait terminates outright, which is the conventional
    reading of a repeated signal and the escape hatch if the join ever wedges.

    The handler is dispatched to its own thread because it must be: signals are
    delivered to the main thread, `shutdown()` blocks until `serve_forever`
    returns, and `serve_forever` is what the main thread is inside. Calling it
    directly from the handler would deadlock the process.
    """

    def request_stop(signum: int, frame: object) -> None:  # noqa: ARG001
        logger.info("收到 SIGTERM，停止接受新请求并等在飞请求收尾")
        threading.Thread(
            target=httpd.shutdown, name="wt-media-sigterm", daemon=True
        ).start()

    return signal.signal(signal.SIGTERM, request_stop)


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    bitbrowser: ProfileScanner,
    checkpoint_store: Optional[CheckpointStore] = None,
    state: LocalAgentState,
    auth_token: str = "",
    cloud_base_url: str = "",
) -> None:
    api = LocalApiServer(
        state,
        bitbrowser=bitbrowser,
        checkpoint_store=checkpoint_store,
        auth_token=auth_token,
        cloud_base_url=cloud_base_url,
    )
    httpd = ThreadingHTTPServer((host, port), make_handler(api))
    # **One line, and without it the join below is a no-op.** `ThreadingMixIn`
    # tracks a request thread so `server_close()` can join it, but its tracker
    # refuses daemon threads -- `_Threads.append` returns early `if thread.daemon`
    # -- and `ThreadingHTTPServer` sets `daemon_threads = True`. So the default
    # pair *looks* like "close waits for in-flight requests" (block_on_close is
    # True, and server_close does call `_threads.join()`) while the list being
    # joined is empty. Measured, not read off the class: with a request parked
    # mid-flight the process exited at once, and only after this flag did it wait.
    #
    # The cost of turning it off is that a request that never finishes keeps the
    # interpreter alive at exit. That is the intended trade, and it is not
    # unbounded: the waiting side is Desktop, whose grace window ends in SIGKILL.
    httpd.daemon_threads = False
    logger.info("wt-media-agent local API listening on %s:%s", host, port)
    print(f"wt-media-agent local API listening on {host}:{port}", flush=True)
    # Installed here rather than in `main`, so that "serving" and "answers
    # SIGTERM" are the same promise wherever `serve` is called from. It also
    # means `serve` requires the main thread — `signal.signal` raises otherwise,
    # and that is the right failure: a `serve` that silently cannot be asked to
    # stop is the defect this exists to remove, and it should not be a silent one.
    previous = _answer_sigterm(httpd)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # In-flight requests finish here, not before: `server_close()` joins the
        # request threads, given the flag set above. The handler is restored
        # **after** that join, so a SIGTERM arriving mid-wait is still answered
        # rather than killing a process that is doing what it was asked.
        httpd.server_close()
        signal.signal(signal.SIGTERM, previous)
    # After the close, not before, and that ordering is the whole value of this
    # line: it is what Desktop reads out of the sidecar's buffered output to tell
    # "asked and closed" from "had to be killed". Printed before the join it
    # would mean only "stopped accepting".
    print("wt-media-agent local API stopped", flush=True)


def main(argv: list[str] | None = None) -> int:
    """Frozen console-script entry (`wt-media-local-health`, verify-health.sh).

    The module path and this symbol are load-bearing: `scripts/verify-health.sh`
    and wt-media-workspace's acceptance scripts both invoke
    `python -m wt_media_agent.local_api.server`. Assembly itself lives in
    `bootstrap`, so this stays an entry point rather than a second init path.

    `--host`/`--port` keep their historical defaults for the same reason. The
    runtime token is read from configuration unless a caller passes one
    explicitly; a sidecar never passes one on a command line (it would be
    visible in `ps`).
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="")
    parser.add_argument("--port", default=0, type=int)
    parser.add_argument("--auth-token", default="")
    args = parser.parse_args(argv)

    components = build_components()
    config = components.config
    # No logging call here, deliberately: `build_components()` has already
    # initialized it (bootstrap/app.py), and the second call this used to make
    # was both redundant and invisible. One initializer, in bootstrap --
    # `test_dependency_boundaries.py` R11 keeps it that way.
    serve(
        args.host or config.local_api_host,
        args.port or config.local_api_port,
        bitbrowser=components.bitbrowser,
        checkpoint_store=components.store,
        state=components.state,
        auth_token=args.auth_token or config.runtime_token,
        cloud_base_url=config.cloud_base_url,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
