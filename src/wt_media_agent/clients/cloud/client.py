"""Cloud-Agent API 客户端。

所有调用都以 `Authorization: Bearer <Agent Token>` 认证。浏览器操作不再通过
Cloud 通用任务领取；保留的是 BitBrowser 绑定和独立 file-transfer 租约面。
"""

from __future__ import annotations

import json
import ssl
from typing import Callable, Mapping, Optional
from urllib import error as urlerror
from urllib import request

from wt_media_agent.runtime.constants import PUBLICATION_REPORT_OPERATIONS
from wt_media_agent.clients.cloud.errors import (
    AgentAuthError,
    CloudUnavailableError,
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

    # ---- 准入与绑定 ----

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
            {"bit_main_user_id": main_user_id, "observed_at": observed_at, "rebind": rebind},
            _bearer(token),
        )
        return _expect_data(response)

    # ---- 发布执行（独立于文件下载租约） ----

    def claim_publication(self, token: str, request_key: str, *, capacity=None):
        body = {'request_key': request_key}
        if capacity is not None:
            body['capacity'] = _publication_capacity(capacity)
            body['supported_platforms'] = ['baidu', 'bilibili']
        response = self._transport('POST', '/api/v1/cloud-agent/publication-task/claim', body, _bearer(token))
        _raise_on_failure(response)
        if 'data' not in response:
            raise CloudUnavailableError('publication response missing data')
        data = response['data']
        if data is None:
            return None
        if not isinstance(data, Mapping):
            raise CloudUnavailableError('publication response malformed')
        return dict(data)

    def claim_publication_check(self, token: str, request_key=None, *, capacity=None):
        body = None
        if request_key is not None:
            body = {'request_key': request_key, 'capacity': _publication_capacity(capacity), 'supported_platforms': ['baidu', 'bilibili']}
        response = self._transport('POST', '/api/v1/cloud-agent/publication-task/check/claim',
                                   body, _bearer(token))
        _raise_on_failure(response)
        if 'data' not in response or (response['data'] is not None and not isinstance(response['data'], Mapping)):
            raise CloudUnavailableError('publication check response missing data')
        data = response['data']
        if data is None:
            return None
        if request_key is not None:
            _check_claim_state(data, request_key)
        return dict(data)

    def settle_unreceived_publication_check(self, token: str, request_key: str):
        """Close only an original durable key whose response never reached local storage."""
        data = _expect_data(self._transport('POST', '/api/v1/cloud-agent/publication-task/check/claim',
            {'request_key': request_key, 'settle_unreceived': True}, _bearer(token)))
        if not isinstance(data, Mapping):
            raise CloudUnavailableError('publication check settlement missing acknowledgement')
        _check_claim_state(data, request_key, settlement=True)
        return dict(data)

    def release_publication_check(self, token: str, task_id: str, packet: dict):
        if (packet.get('release_only') is not True or not all(_publication_identity(value) for value in
                (task_id, packet.get('attempt_id'), packet.get('generation'), packet.get('reservation_id')))):
            raise ValueError('only an identified original check settlement is allowed')
        data = _expect_data(self._transport('POST', f'/api/v1/cloud-agent/publication-task/{task_id}/check',
            packet, _bearer(token)))
        _check_release_ack(data, task_id, packet['attempt_id'], packet['generation'], packet['reservation_id'])
        return dict(data)

    def observe_publication(self, token: str, task_id: str, packet: dict):
        for value in (task_id, packet.get('generation'), packet.get('observation', {}).get('attempt_id'),
                      packet.get('observation', {}).get('expected_revision')):
            if (not isinstance(value, str) or not 0 < len(value) <= 20 or not value.isascii()
                    or not value.isdecimal() or str(int(value)) != value
                    or not 0 < int(value) <= 18446744073709551615):
                raise ValueError('canonical publication check identity required')
        acknowledgement = dict(_expect_data(self._transport('POST',
            f'/api/v1/cloud-agent/publication-task/{task_id}/check', packet, _bearer(token))))
        if (acknowledgement.get('publication_id'), acknowledgement.get('attempt_id'), acknowledgement.get('generation')) != (
                task_id, packet['observation']['attempt_id'], packet['generation']):
            raise CloudUnavailableError('publication observation acknowledgement does not match')
        if 'reservation_id' in packet:
            _check_release_ack(acknowledgement, task_id, packet['observation']['attempt_id'],
                packet['generation'], packet['reservation_id'])
        observation_hash = acknowledgement.get('observation_hash')
        if (not isinstance(observation_hash, str) or len(observation_hash) != 64
                or any(char not in '0123456789abcdef' for char in observation_hash)):
            raise CloudUnavailableError('publication observation acknowledgement is missing its hash')
        return acknowledgement

    def report_publication(self, token: str, task_id: str, operation: str, packet: dict):
        if operation not in PUBLICATION_REPORT_OPERATIONS:
            raise ValueError('invalid publication report operation')
        if not isinstance(task_id, str) or not task_id.isascii() or not task_id.isdecimal() or str(int(task_id)) != task_id or int(task_id) <= 0:
            raise ValueError('canonical task identity required')
        return dict(_expect_data(self._transport('POST',
            f'/api/v1/cloud-agent/publication-task/{task_id}/{operation}', packet, _bearer(token))))

    def original_publication_attempt(self, token: str, task_id: str, attempt_id: str):
        for value in (task_id, attempt_id):
            if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or str(int(value)) != value or int(value) <= 0:
                raise ValueError('canonical publication identity required')
        return dict(_expect_data(self._transport('GET',
            f'/api/v1/cloud-agent/publication-task/{task_id}/attempt/{attempt_id}', None, _bearer(token))))

    def publication_human(self,token,task_id,attempt_id,revision,*,resume,capacity=None):
        for value in (task_id,attempt_id,revision):
            if not isinstance(value,str) or not value.isascii() or not value.isdecimal() or str(int(value)) != value or not 0 < int(value) <= 18446744073709551615:
                raise ValueError('canonical publication identity required')
        path = f'/api/v1/cloud-agent/publication-task/{task_id}'
        if resume:
            method,path,body = 'POST',path+'/resume',{'attempt_id':attempt_id,'expected_revision':revision}
            if capacity is not None:
                body['capacity'] = _publication_capacity(capacity)
        else:
            method,path,body = 'GET',path+f'/attempt/{attempt_id}/scene?expected_revision={revision}',None
        return dict(_expect_data(self._transport(method,path,body,_bearer(token))))

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
        payload: Mapping[str, object] | None,
        headers: Mapping[str, str],
    ) -> str:
        body = None if method == "GET" else json.dumps(payload, separators=(",", ":")).encode("utf-8")
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


def _publication_capacity(value):
    if (not isinstance(value, Mapping) or set(value) != {'automatic_slots', 'window_slots'}
            or any(type(item) is not int or not 1 <= item <= 65535 for item in value.values())):
        raise ValueError('publication capacity requires two positive bounded integers')
    return dict(value)


def _check_release_ack(data, task_id, attempt_id, generation, reservation_id):
    if (not isinstance(data, Mapping) or data.get('released') is not True
            or (data.get('publication_id'), data.get('attempt_id'), data.get('generation'), data.get('reservation_id'))
            != (task_id, attempt_id, generation, reservation_id)):
        raise CloudUnavailableError('publication check settlement acknowledgement differs')


def _check_claim_state(data, request_key, *, settlement=False):
    state = data.get('reservation_state')
    if state in {'released', 'cancelled'}:
        if any(key in data for key in ('task', 'attempt', 'expected_revision')):
            raise CloudUnavailableError('settled publication check cannot carry executable input')
        if state == 'cancelled':
            if data.get('request_key') != request_key:
                raise CloudUnavailableError('cancelled publication check key differs')
        elif (data.get('replayed') is not True or data.get('released') is not True
                or not all(_publication_identity(data.get(key)) for key in
                    ('publication_id', 'attempt_id', 'generation', 'reservation_id'))):
            raise CloudUnavailableError('released publication check has no original acknowledgement')
    elif state != 'active' or settlement:
        raise CloudUnavailableError('publication check reservation state is invalid')


def _publication_identity(value):
    return (isinstance(value, str) and value.isascii() and value.isdecimal()
            and 0 < len(value) <= 20 and str(int(value)) == value and 0 < int(value) < 2**64)
