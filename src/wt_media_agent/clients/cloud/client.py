"""Cloud-Agent API client used by Agent runtime slices."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Mapping, Optional
from urllib import error as urlerror
from urllib import request

from wt_media_agent.clients.cloud.contract import (
    EXPECTED_MAJOR_VERSION,
    REQUIRED_CONTRACT_REVISION,
)
from wt_media_agent.clients.cloud.transfer import (
    TransferIntegrityRejectedError,
    TransferLease,
    TransferLeaseLostError,
    TransferTransport,
    TransferUnavailableError,
    completion_body,
    parse_lease,
    parse_terminal,
)


Transport = Callable[
    [str, str, Mapping[str, object], Mapping[str, str]], Mapping[str, object]
]


class SessionInvalidError(RuntimeError):
    """Cloud invalidated the user session bound to this Local Agent."""


@dataclass(frozen=True)
class AgentIdentity:
    agent_id: str
    mode: str
    version: str = "0.1.0"
    capabilities: tuple[str, ...] = ()


#: What this client used to hard-code. Kept as the default so that a caller
#: who does not configure a timeout keeps the previous behaviour.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10


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
        # A second transport rather than a wider one: the transfer endpoints put
        # their meaning in the status line and answer `200` with no body at all,
        # while every method above them answers with a body in the cases that
        # matter. See `clients/cloud/transfer.py`.
        self._transfer_transport = transfer_transport or self._http_transfer_transport

    def register(self, identity: AgentIdentity) -> Mapping[str, object]:
        payload = {
            "agent_id": identity.agent_id,
            "mode": identity.mode,
            "version": identity.version,
            "contract_major_version": EXPECTED_MAJOR_VERSION,
            "contract_revision": REQUIRED_CONTRACT_REVISION,
            "capabilities": list(identity.capabilities),
        }
        response = self._transport("POST", "/api/v1/cloud-agent/agents/register", payload, {})
        return _expect_data(response)

    def heartbeat(self, agent_id: str, status: str = "online") -> Mapping[str, object]:
        response = self._transport(
            "POST",
            f"/api/v1/cloud-agent/agents/{agent_id}/heartbeat",
            {"status": status},
            {},
        )
        return _expect_data(response)

    def claim_task(self, agent_id: str, lease_seconds: int = 60) -> Mapping[str, object]:
        response = self._transport(
            "POST",
            "/api/v1/cloud-agent/tasks/claim",
            {"agent_id": agent_id, "lease_seconds": lease_seconds},
            {},
        )
        return _expect_data(response)

    def report_task(
        self,
        task_id: str,
        agent_id: str,
        status: str,
        progress: int,
        message: str = "",
        result: Optional[Mapping[str, object]] = None,
    ) -> Mapping[str, object]:
        response = self._transport(
            "POST",
            f"/api/v1/cloud-agent/tasks/{task_id}/report",
            {
                "agent_id": agent_id,
                "status": status,
                "progress": progress,
                "message": message,
                **({"result": dict(result)} if result is not None else {}),
            },
            {},
        )
        return _expect_data(response)

    def register_local(
        self,
        binding_token: str,
        identity: AgentIdentity,
        *,
        device_id: str,
    ) -> Mapping[str, object]:
        if identity.mode != "local":
            raise ValueError("local registration requires local Agent mode")
        payload = {
            "binding_token": binding_token,
            "agent_id": identity.agent_id,
            "device_id": device_id,
            "agent_version": identity.version,
            "contract_major_version": EXPECTED_MAJOR_VERSION,
            "contract_revision": REQUIRED_CONTRACT_REVISION,
        }
        response = self._transport(
            "POST", "/api/v1/local-agent/nodes/register", payload, {}
        )
        return _expect_data(response)

    def report_runtime(
        self,
        node_id: str,
        node_credential: str,
        report: Mapping[str, object],
    ) -> Mapping[str, object]:
        response = self._transport(
            "POST",
            f"/api/v1/local-agent/nodes/{node_id}/runtime-report",
            report,
            {"authorization": f"Bearer {node_credential}"},
        )
        return _expect_data(response)

    def preflight_sensitive_task(
        self,
        node_id: str,
        node_credential: str,
        task_id: str,
    ) -> Mapping[str, object]:
        response = self._transport(
            "POST",
            f"/api/v1/local-agent/sensitive-tasks/{task_id}/preflight",
            {"node_id": node_id},
            {"authorization": f"Bearer {node_credential}"},
        )
        return _expect_data(response)

    def finish_sensitive_permit(
        self,
        node_id: str,
        node_credential: str,
        permit_id: str,
        permit_credential: str,
        outcome: str,
    ) -> Mapping[str, object]:
        response = self._transport(
            "POST",
            f"/api/v1/local-agent/sensitive-permits/{permit_id}/finish",
            {"outcome": outcome, "node_id": node_id},
            {
                "authorization": f"Bearer {node_credential}",
                "x-profile-permit": permit_credential,
            },
        )
        return _expect_data(response)

    def renew_sensitive_permit(
        self,
        node_id: str,
        node_credential: str,
        permit_id: str,
        permit_credential: str,
        lease_seconds: int = 60,
    ) -> Mapping[str, object]:
        response = self._transport(
            "POST",
            f"/api/v1/local-agent/sensitive-permits/{permit_id}/renew",
            {"node_id": node_id, "lease_seconds": lease_seconds},
            {
                "authorization": f"Bearer {node_credential}",
                "x-profile-permit": permit_credential,
            },
        )
        return _expect_data(response)

    # ---- file-transfer tasks ----
    #
    # The node credential is the whole identity on these four calls: `claim`
    # carries no request body and no path parameter, so Cloud can only know who
    # is asking from the bearer header. That is also why none of these methods
    # builds a message out of the credential.

    def claim_transfer_task(self, node_credential: str) -> Optional[TransferLease]:
        """Lease one pending transfer for this node, or `None` if there is none.

        A `null` task is an ordinary answer -- it means nothing is waiting -- and
        not a failure, so it is returned rather than raised.
        """
        status, body = self._transfer_transport(
            "POST",
            "/api/v1/cloud-agent/file-transfer-tasks/claim",
            {},
            _node_bearer(node_credential),
        )
        _raise_for_transfer_status(status, body)
        task = _expect_data(body).get("task")
        if task is None:
            return None
        return parse_lease(task)

    def heartbeat_transfer_task(
        self, node_credential: str, task_id: str, completed_bytes: int
    ) -> None:
        """Renew the lease. This, and not `progress`, is what keeps it alive."""
        status, body = self._transfer_transport(
            "POST",
            f"/api/v1/cloud-agent/file-transfer-tasks/{task_id}/heartbeat",
            {"completed_bytes": int(completed_bytes)},
            _node_bearer(node_credential),
        )
        _raise_for_transfer_status(status, body)

    def report_transfer_progress(
        self,
        node_credential: str,
        task_id: str,
        completed_bytes: int,
        bytes_per_second: int,
    ) -> None:
        """Record progress. The response carries no body to read."""
        status, body = self._transfer_transport(
            "POST",
            f"/api/v1/cloud-agent/file-transfer-tasks/{task_id}/progress",
            {
                "completed_bytes": int(completed_bytes),
                "bytes_per_second": int(bytes_per_second),
            },
            _node_bearer(node_credential),
        )
        _raise_for_transfer_status(status, body)

    def complete_transfer_task(
        self,
        node_credential: str,
        task_id: str,
        *,
        status: str,
        completed_bytes: Optional[int] = None,
        sha256: Optional[str] = None,
        file_name: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> TransferTerminal:
        """Record the terminal outcome, and return what Cloud stored for it."""
        payload = completion_body(
            status=status,
            completed_bytes=completed_bytes,
            sha256=sha256,
            file_name=file_name,
            error_code=error_code,
            error_message=error_message,
        )
        http_status, body = self._transfer_transport(
            "POST",
            f"/api/v1/cloud-agent/file-transfer-tasks/{task_id}/complete",
            payload,
            _node_bearer(node_credential),
        )
        _raise_for_transfer_status(http_status, body)
        return parse_terminal(_expect_data(body))

    def _http_transport(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object],
        headers: Mapping[str, str],
    ) -> Mapping[str, object]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        req = request.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={"content-type": "application/json", **headers},
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urlerror.HTTPError as exc:
            raw = exc.read().decode("utf-8")
        decoded = json.loads(raw)
        if not isinstance(decoded, Mapping):
            raise ValueError("cloud response must be a JSON object")
        return decoded

    def _http_transfer_transport(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object],
        headers: Mapping[str, str],
    ) -> tuple[int, Mapping[str, object]]:
        """Like `_http_transport`, but the status is part of the answer.

        Two differences from the transport above. The status is returned rather
        than collapsed into "a body", because on this surface the status is the
        whole message -- `heartbeat` and `progress` answer `200` with no content
        and `409` with the refusal, and both reach here as no useful body. And an
        empty body is therefore ordinary: it means the call succeeded.
        """
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
        # A `200` from heartbeat or progress has no content at all, so an empty
        # body is a success and not something to parse.
        decoded = json.loads(raw) if raw.strip() else {}
        if not isinstance(decoded, Mapping):
            raise ValueError("cloud response must be a JSON object")
        return status, decoded


def _node_bearer(node_credential: str) -> dict[str, str]:
    """The only place the node credential is put on the wire."""
    return {"authorization": f"Bearer {node_credential}"}


def _raise_for_transfer_status(status: int, body: Mapping[str, object]) -> None:
    """Turn a transfer refusal into the type its caller has to act on.

    The status decides, not the body: the frozen contract pins `409` and `422`
    and defines no body for either, so a client that read the meaning out of the
    body would be relying on something no contract states. Cloud's own `message`
    is carried along because it is the operator's only description of the
    refusal, and this module never adds the credential to it.
    """
    if 200 <= status < 300:
        return
    message = body.get("message")
    detail = str(message) if isinstance(message, str) and message else ""
    suffix = f": {detail}" if detail else ""
    if status == 401:
        raise SessionInvalidError(
            f"Cloud refused this node's credential (HTTP 401){suffix}"
        )
    if status == 409:
        raise TransferLeaseLostError(f"the transfer lease is gone (HTTP 409){suffix}")
    if status == 422:
        raise TransferIntegrityRejectedError(
            f"Cloud rejected the completed transfer (HTTP 422){suffix}"
        )
    raise TransferUnavailableError(f"the transfer endpoint answered HTTP {status}{suffix}")


def _expect_data(response: Mapping[str, object]) -> Mapping[str, object]:
    if response.get("errcode") == 11001:
        raise SessionInvalidError(str(response.get("message") or "Cloud session is invalid"))
    data = response.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("cloud response missing data object")
    return data
