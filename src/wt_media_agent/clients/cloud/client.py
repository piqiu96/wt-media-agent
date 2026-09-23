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


class CloudAgentClient:
    def __init__(self, base_url: str, transport: Optional[Transport] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._transport = transport or self._http_transport

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
            with request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
        except urlerror.HTTPError as exc:
            raw = exc.read().decode("utf-8")
        decoded = json.loads(raw)
        if not isinstance(decoded, Mapping):
            raise ValueError("cloud response must be a JSON object")
        return decoded


def _expect_data(response: Mapping[str, object]) -> Mapping[str, object]:
    if response.get("errcode") == 11001:
        raise SessionInvalidError(str(response.get("message") or "Cloud session is invalid"))
    data = response.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("cloud response missing data object")
    return data
