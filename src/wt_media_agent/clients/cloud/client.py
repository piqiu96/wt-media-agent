"""Cloud-Agent API 客户端。

所有 Agent 路由都以 `Authorization: Bearer <Agent Token>` 认证，身份由 Token
的 `sub` 决定；请求体里不再有 `agent_id`/`node_id`。已领取任务的后续调用必须
带 `attempt_id`，Cloud 用它拒绝被新 attempt 取代的旧结果。
"""

from __future__ import annotations

import json
import ssl
from typing import Callable, Mapping, Optional
from urllib import error as urlerror
from urllib import request

from wt_media_agent.clients.cloud.errors import (
    AgentAuthError,
    CloudUnavailableError,
    NoPendingTaskError,
    failure_from,
)
from wt_media_agent.clients.cloud.transfer import (
    TransferIntegrityRejectedError,
    TransferLease,
    TransferLeaseLostError,
    TransferTerminal,
    TransferTransport,
    TransferUnavailableError,
    completion_body,
    parse_lease,
    parse_terminal,
)


Transport = Callable[[str, str, Mapping[str, object], Mapping[str, str]], Mapping[str, object]]

#: 默认请求超时。
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10

#: 未携带 Token 时仍可调用的路由只有空实现；调用方负责判断 Token 是否存在。
_TOKEN_HEADER = "authorization"


class CloudAgentClient:
    def __init__(
        self,
        base_url: str,
        transport: Optional[Transport] = None,
        *,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        transfer_transport: Optional[TransferTransport] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport or self._http_transport
        # 传输面的含义在 HTTP 状态上，且 heartbeat/progress 成功时没有 body，
        # 所以单独用一个返回状态码的 transport。
        self._transfer_transport = transfer_transport or self._http_transfer_transport

    # ---- 普通任务 ----

    def claim_task(self, token: str, lease_seconds: int = 60) -> Optional[Mapping[str, object]]:
        """领取一个任务；没有可领取任务时返回 None。"""
        response = self._transport(
            "POST",
            "/api/v1/cloud-agent/tasks/claim",
            {"lease_seconds": int(lease_seconds)},
            _bearer(token),
        )
        try:
            data = _expect_data(response)
        except NoPendingTaskError:
            return None
        if not data.get("task_id"):
            return None
        return data

    def report_task(
        self,
        token: str,
        task_id: str,
        attempt_id: str,
        *,
        status: str,
        progress: int = 0,
        message: str = "",
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        retryable: Optional[bool] = None,
        recovery_action: Optional[str] = None,
        result: Optional[Mapping[str, object]] = None,
    ) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "attempt_id": attempt_id,
            "status": status,
            "progress": int(progress),
            "message": message,
        }
        if error_code is not None:
            payload["error_code"] = error_code
        if error_message is not None:
            payload["error_message"] = error_message
        if retryable is not None:
            payload["retryable"] = retryable
        if recovery_action is not None:
            payload["recovery_action"] = recovery_action
        if result is not None:
            payload["result"] = dict(result)
        response = self._transport(
            "POST",
            f"/api/v1/cloud-agent/tasks/{task_id}/report",
            payload,
            _bearer(token),
        )
        return _expect_data(response)

    def report_activity(
        self,
        token: str,
        task_id: str,
        attempt_id: str,
        *,
        message: str = "",
        stage: str = "",
    ) -> None:
        """刷新 last_report_at。Cloud 成功时返回 `data: null`。"""
        payload: dict[str, object] = {"attempt_id": attempt_id}
        if message:
            payload["message"] = message
        if stage:
            payload["stage"] = stage
        response = self._transport(
            "POST",
            f"/api/v1/cloud-agent/tasks/{task_id}/activity",
            payload,
            _bearer(token),
        )
        _raise_on_failure(response)
        return None

    # ---- 准入与绑定 ----

    def execution_preflight(
        self,
        token: str,
        *,
        action: str,
        task_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        profile_id: Optional[str] = None,
        media_account_id: Optional[str] = None,
        facts: Optional[Mapping[str, object]] = None,
    ) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "action": action,
            "task_id": task_id or "",
            "attempt_id": attempt_id or "",
            "profile_id": profile_id or "",
            "media_account_id": media_account_id or "",
        }
        if facts is not None:
            payload["facts"] = dict(facts)
        response = self._transport(
            "POST", "/api/v1/local-agent/execution-preflight", payload, _bearer(token)
        )
        return _expect_data(response)

    def bind_bit_account(
        self,
        token: str,
        *,
        main_user_id: str,
        observed_at: str,
        rebind: bool = False,
    ) -> Mapping[str, object]:
        response = self._transport(
            "POST",
            "/api/v1/local-agent/bit-account/bind",
            {"main_user_id": main_user_id, "observed_at": observed_at, "rebind": rebind},
            _bearer(token),
        )
        return _expect_data(response)

    # ---- 文件传输 ----
    #
    # 四个调用的身份都在 Bearer Token 上；已领取后还必须带 claim 返回的
    # attempt_id，Cloud 以它判断 lease 是否仍归这次执行。

    def claim_transfer_task(self, token: str) -> Optional[TransferLease]:
        """领取一个待下载任务；没有时返回 None。"""
        status, body = self._transfer_transport(
            "POST", "/api/v1/cloud-agent/file-transfer-tasks/claim", {}, _bearer(token)
        )
        _raise_for_transfer_status(status, body)
        task = (_expect_data(body) or {}).get("task")
        if task is None:
            return None
        return parse_lease(task)

    def heartbeat_transfer_task(
        self, token: str, task_id: str, attempt_id: str, completed_bytes: int
    ) -> None:
        """续租；真正保活的是它，不是 progress。"""
        status, body = self._transfer_transport(
            "POST",
            f"/api/v1/cloud-agent/file-transfer-tasks/{task_id}/heartbeat",
            {"attempt_id": attempt_id, "completed_bytes": int(completed_bytes)},
            _bearer(token),
        )
        _raise_for_transfer_status(status, body)

    def report_transfer_progress(
        self,
        token: str,
        task_id: str,
        attempt_id: str,
        completed_bytes: int,
        bytes_per_second: int,
    ) -> None:
        status, body = self._transfer_transport(
            "POST",
            f"/api/v1/cloud-agent/file-transfer-tasks/{task_id}/progress",
            {
                "attempt_id": attempt_id,
                "completed_bytes": int(completed_bytes),
                "bytes_per_second": int(bytes_per_second),
            },
            _bearer(token),
        )
        _raise_for_transfer_status(status, body)

    def complete_transfer_task(
        self,
        token: str,
        task_id: str,
        attempt_id: str,
        *,
        status: str,
        completed_bytes: Optional[int] = None,
        sha256: Optional[str] = None,
        file_name: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> TransferTerminal:
        """写入终态，并返回 Cloud 实际保存的结果。"""
        payload = completion_body(
            status=status,
            completed_bytes=completed_bytes,
            sha256=sha256,
            file_name=file_name,
            error_code=error_code,
            error_message=error_message,
        )
        payload["attempt_id"] = attempt_id
        http_status, body = self._transfer_transport(
            "POST",
            f"/api/v1/cloud-agent/file-transfer-tasks/{task_id}/complete",
            payload,
            _bearer(token),
        )
        _raise_for_transfer_status(http_status, body)
        return parse_terminal(_expect_data(body))

    # ---- HTTP ----

    def _http_transport(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object],
        headers: Mapping[str, str],
    ) -> Mapping[str, object]:
        raw = self._send(method, path, payload, headers)
        return _decode_envelope(raw)

    def _http_transfer_transport(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object],
        headers: Mapping[str, str],
    ) -> tuple[int, Mapping[str, object]]:
        """返回 HTTP 状态与解码后的 body；成功但无内容时 body 为空对象。"""
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        req = request.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={"content-type": "application/json", **headers},
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                status = int(resp.status)
                raw = resp.read().decode("utf-8")
        except urlerror.HTTPError as exc:
            status = int(exc.code)
            raw = exc.read().decode("utf-8")
        except (urlerror.URLError, TimeoutError, OSError) as exc:
            # 传输面的调用方按 `TransferUnavailableError` 决定是否重试，所以这里
            # 不抛普通传输层的错误；异常原文可能带地址，消息只用固定句子。
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise TransferUnavailableError(
                    "Cloud TLS certificate verification failed"
                ) from exc
            raise TransferUnavailableError(
                "the transfer endpoint could not be reached"
            ) from exc
        if not raw.strip():
            return status, {}
        return status, _decode_envelope(raw)

    def _send(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object],
        headers: Mapping[str, str],
    ) -> str:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        req = request.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={"content-type": "application/json", **headers},
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8")
        except urlerror.HTTPError as exc:
            return exc.read().decode("utf-8")
        except (urlerror.URLError, TimeoutError, OSError) as exc:
            raise CloudUnavailableError("cloud could not be reached") from exc


def _decode_envelope(raw: str) -> Mapping[str, object]:
    try:
        decoded = json.loads(raw)
    except ValueError as exc:
        raise CloudUnavailableError("cloud answered something that is not its envelope") from exc
    if not isinstance(decoded, Mapping):
        raise CloudUnavailableError("cloud response must be a JSON object")
    return decoded


def _bearer(token: str) -> dict[str, str]:
    return {_TOKEN_HEADER: f"Bearer {token}"}


def _raise_on_failure(response: Mapping[str, object], status: Optional[int] = None) -> None:
    failure = failure_from(response, status)
    if failure is not None:
        raise failure


def _expect_data(response: Mapping[str, object]) -> Mapping[str, object]:
    _raise_on_failure(response)
    data = response.get("data")
    if not isinstance(data, Mapping):
        # 网关/代理的错误应答没有 Cloud 信封（也就没有 errcode 可判断），
        # 按“应答不可信”归到传输失败，进度上报才能只记日志而不打断执行。
        raise CloudUnavailableError("cloud response missing data object")
    return data


def _raise_for_transfer_status(status: int, body: Mapping[str, object]) -> None:
    """按状态把传输拒绝转成调用方能处理的具体类型。

    Cloud 的错误码优先（409 `STALE_ATTEMPT`、401 的新码）；没有码时按状态归类，
    因为 contract 对 409/422 不要求 body。
    """
    failure = failure_from(body, status)
    if failure is None:
        return
    message = str(body.get("message") or failure)
    if isinstance(failure, AgentAuthError):
        raise failure
    if status == 409:
        raise TransferLeaseLostError(f"the transfer lease is gone (HTTP 409): {message}")
    if status == 422:
        raise TransferIntegrityRejectedError(
            f"Cloud rejected the completed transfer (HTTP 422): {message}"
        )
    raise TransferUnavailableError(f"the transfer endpoint answered HTTP {status}") from failure
