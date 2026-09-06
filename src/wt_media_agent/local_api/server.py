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
from urllib import parse as urlparse
from urllib import request as urlrequest

from wt_media_agent.local_api.state import LocalAgentState
from wt_media_agent.storage.checkpoint_store import CheckpointStore
from wt_media_agent.runtimes import cdp_client
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
    def group_list(self) -> list[dict[str, object]]: ...
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
            address = _parse_first_proxy_address(raw, protocol)
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
                self.bitbrowser.update_profile(profile_id, {"proxyType": "noproxy", "proxyMethod": 2})
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
            cookies = _read_account_cookies(self.bitbrowser, profile_id, devtools)
        except BitBrowserIdentityError:
            return 409, {"error": {"code": "bitbrowser_identity_unverifiable"}}
        except BitBrowserResponseError as e:
            return 502, {"error": {"code": "bitbrowser_response_error", "message": str(e)}}

        nav_data = None
        if platform == "bilibili" and devtools:
            nav_data = _bilibili_nav_via_cdp(devtools)

        if platform == "baijiahao":
            result = _identify_baijiahao(cookies)
        elif platform == "bilibili":
            result = _identify_bilibili(cookies, nav_data)
        else:
            result = _identify_platform_account(platform, cookies)
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


def _parse_first_proxy_address(raw: str, default_protocol: str) -> dict[str, object]:
    for line in raw.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        parsed = _parse_proxy_address(candidate, default_protocol)
        if parsed is not None:
            return parsed
    raise ValueError("no supported proxy address")


def _parse_proxy_address(raw: str, default_protocol: str) -> dict[str, object] | None:
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


def _read_account_cookies(bitbrowser, profile_id: str, devtools: str) -> list[dict[str, object]]:
    """Read the profile's cookies: live via CDP when a DevTools endpoint is available,
    otherwise fall back to /browser/detail saved cookies."""
    if devtools:
        try:
            return cdp_client.read_live_cookies(devtools)
        except Exception:  # noqa: BLE001 - fall back to saved cookies
            pass
    return bitbrowser.read_cookies(profile_id)


def _bilibili_nav_via_cdp(devtools: str) -> dict[str, object] | None:
    """Fetch Bilibili nav (mid/uname/face) from inside the page to bypass risk control."""
    try:
        nav = cdp_client.eval_fetch_json(devtools, "https://api.bilibili.com/x/web-interface/nav")
        if nav.get("status") == 200:
            return nav.get("json") or {}
    except Exception:  # noqa: BLE001 - best-effort, never block identification
        pass
    return None


def _build_account_check_items(result: dict[str, object], has_expected: bool) -> list[dict[str, object]]:
    """Build check items 5-8 for the account check result (1-4 are Cloud-side).

    5=平台登录状态 6=登录账号与台账一致 7=需验证码/安全验证 8=账号限制/封号.
    7/8 判定需真实受限账号样本对齐，当前标记为 na（不适用/待对齐）。
    """
    uid = str(result.get("platform_account_id") or "").strip()
    login_status = str(result.get("login_status") or "")
    platform_login = "pass" if uid else ("fail" if login_status == "not_logged_in" else "na")
    platform_login_msg = "已读取到平台账号" if uid else ("未登录" if login_status == "not_logged_in" else "未获取到平台身份")

    if login_status == "account_mismatch":
        account_match = "fail"
        account_match_msg = "当前窗口登录账号与台账不一致"
    elif has_expected and uid:
        account_match = "pass"
        account_match_msg = "登录账号与台账一致"
    elif not uid:
        account_match = "na"
        account_match_msg = "未获取到平台身份，无法校验"
    else:
        account_match = "pass"
        account_match_msg = "登录账号与台账一致"

    return [
        {"key": "platform_login", "label": "平台登录状态", "status": platform_login, "message": platform_login_msg},
        {"key": "account_match", "label": "登录账号与台账一致", "status": account_match, "message": account_match_msg},
        {"key": "verification_needed", "label": "需验证码/安全验证", "status": "na", "message": "需真实受限账号样本对齐"},
        {"key": "account_restricted", "label": "账号限制/封号", "status": "na", "message": "需真实受限账号样本对齐"},
    ]


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


def _identify_baijiahao(cookies: list[dict[str, object]]) -> dict[str, object]:
    """Identify a Baijiahao/Baidu account via the public logininfo API.

    Baijiahao UID is not derivable from cookies alone (BDUSS is encrypted), so
    the Agent calls image.baidu.com/user/logininfo server-side with the BDUSS
    cookie to read uid / nickname / portrait.
    """
    cookie_by_name: dict[str, str] = {}
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name", "")).strip()
        value = str(cookie.get("value", "")).strip()
        if name and value:
            cookie_by_name[name] = value
    bduss = cookie_by_name.get("BDUSS", "").strip()
    if not bduss:
        return {
            "platform_account_id": "",
            "name": "",
            "avatar_url": "",
            "login_status": "not_logged_in",
            "message": "未读取到百度登录凭证(BDUSS)",
        }
    try:
        url = f"https://image.baidu.com/user/logininfo?time={int(time.time() * 1000)}&src=pc&page=index"
        req = urlrequest.Request(
            url,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://image.baidu.com/",
                "Cookie": f"BDUSS={urlparse.quote(bduss)}",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
                ),
            },
        )
        body = urlrequest.urlopen(req, timeout=10).read().decode(errors="replace")
        payload = json.loads(body)
        status = payload.get("status") or {}
        if status.get("code") != 0:
            return {
                "platform_account_id": "",
                "name": "",
                "avatar_url": "",
                "login_status": "expired",
                "message": f"百度登录校验失败: {status.get('msg', '')}",
            }
        user = payload.get("data", {}).get("user") or {}
        uid = str(user.get("user_id", "") or "").strip()
        name = str(user.get("user_name", "") or "").strip()
        portrait = str(user.get("portrait", "") or "").strip()
        if not uid:
            return {
                "platform_account_id": "",
                "name": "",
                "avatar_url": "",
                "login_status": "not_logged_in",
                "message": "未读取到百度账号UID",
            }
        avatar_url = f"https://himg.bdimg.com/sys/portraitn/item/{portrait}" if portrait else ""
        return {
            "platform_account_id": uid,
            "name": name,
            "avatar_url": avatar_url,
            "login_status": "normal",
            "message": "已读取到百家号账号信息",
        }
    except Exception as exc:  # noqa: BLE001 - surface readable message, never leak cookie
        return {
            "platform_account_id": "",
            "name": "",
            "avatar_url": "",
            "login_status": "environment_error",
            "message": f"百家号信息接口调用失败: {exc}",
        }


def _identify_bilibili(cookies: list[dict[str, object]], nav_data: dict[str, object] | None = None) -> dict[str, object]:
    """Identify a Bilibili account from cookies.

    UID comes reliably from the DedeUserID cookie. Nickname/avatar come from the
    in-page nav fetch (nav_data) when available; Bilibili's newer API enforces
    bili_ticket / fingerprint risk control that rejects server-side requests
    (-101 / -799), so a failed nav never downgrades a valid DedeUserID login.
    """
    cookie_by_name: dict[str, str] = {}
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name", "")).strip()
        value = str(cookie.get("value", "")).strip()
        if name and value:
            cookie_by_name[name] = value
    uid = cookie_by_name.get("DedeUserID", "").strip()
    if not uid:
        return {
            "platform_account_id": "",
            "name": "",
            "avatar_url": "",
            "login_status": "not_logged_in",
            "message": "未读取到B站登录Cookie(DedeUserID)",
        }
    name = ""
    face = ""
    nav = nav_data if isinstance(nav_data, dict) else None
    if nav and nav.get("code") == 0:
        data = nav.get("data") or {}
        name = str(data.get("uname", "") or "").strip()
        face = str(data.get("face", "") or "").strip()
    return {
        "platform_account_id": uid,
        "name": name,
        "avatar_url": face,
        "login_status": "normal",
        "message": "已读取到B站账号UID" + ("" if name else "（昵称/头像需页面内验证）"),
    }


def _safe_groups(groups: list[dict[str, object]]) -> list[dict[str, str]]:
    safe: list[dict[str, str]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_id = str(group.get("id") or group.get("groupId") or "").strip()
        group_name = str(group.get("name") or group.get("groupName") or "").strip()
        if group_id:
            safe.append({"id": group_id, "name": group_name or group_id})
    return safe


def _duration_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


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
    logging.basicConfig(
        level=os.getenv("WT_MEDIA_AGENT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--auth-token", default="")
    args = parser.parse_args(argv)
    serve(args.host, args.port, auth_token=args.auth_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
