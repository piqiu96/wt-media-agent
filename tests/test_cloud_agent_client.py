from __future__ import annotations

import unittest
from typing import Mapping

from wt_media_agent.cloud_agent_client import AgentIdentity, CloudAgentClient
from wt_media_agent.cloud_agent_contract import (
    EXPECTED_MAJOR_VERSION,
    REQUIRED_CONTRACT_REVISION,
)


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Mapping[str, object], Mapping[str, str]]] = []

    def __call__(self, method: str, path: str, payload: Mapping[str, object], headers: Mapping[str, str]) -> Mapping[str, object]:
        self.calls.append((method, path, payload, headers))
        return {
            "data": {
                "agent_id": payload.get("agent_id", "agent-local-1"),
                "mode": payload.get("mode", "local"),
                "version": payload.get("version", "0.1.0"),
                "contract_major_version": EXPECTED_MAJOR_VERSION,
                "contract_revision": REQUIRED_CONTRACT_REVISION,
                "status": payload.get("status", "online"),
                "registered_at": "2026-07-14T08:00:00Z",
                "last_heartbeat_at": "2026-07-14T08:00:00Z",
                "capabilities": payload.get("capabilities", []),
            }
        }


class CloudAgentClientTest(unittest.TestCase):
    def test_register_sends_identity_and_contract_facts(self) -> None:
        transport = RecordingTransport()
        client = CloudAgentClient("http://cloud.test", transport=transport)

        node = client.register(
            AgentIdentity(
                agent_id="agent-local-1",
                mode="local",
                capabilities=("noop",),
            )
        )

        self.assertEqual(node["agent_id"], "agent-local-1")
        self.assertEqual(transport.calls[0][0], "POST")
        self.assertEqual(transport.calls[0][1], "/api/v1/cloud-agent/agents/register")
        payload = transport.calls[0][2]
        self.assertEqual(payload["contract_major_version"], EXPECTED_MAJOR_VERSION)
        self.assertEqual(payload["contract_revision"], REQUIRED_CONTRACT_REVISION)
        self.assertEqual(payload["capabilities"], ["noop"])

    def test_heartbeat_sends_status(self) -> None:
        transport = RecordingTransport()
        client = CloudAgentClient("http://cloud.test", transport=transport)

        node = client.heartbeat("agent-local-1", status="draining")

        self.assertEqual(node["status"], "draining")
        self.assertEqual(
            transport.calls[0][1],
            "/api/v1/cloud-agent/agents/agent-local-1/heartbeat",
        )
        self.assertEqual(transport.calls[0][2], {"status": "draining"})

    def test_register_local_consumes_binding_token_without_reusing_session_cookie(self) -> None:
        calls: list[tuple[str, str, Mapping[str, object], Mapping[str, str]]] = []

        def transport(method: str, path: str, payload: Mapping[str, object], headers: Mapping[str, str]) -> Mapping[str, object]:
            calls.append((method, path, payload, headers))
            return {"data": {"node": {"id": "node-1", "user_id": "user-1"}, "node_credential": "node-secret"}}

        client = CloudAgentClient("http://cloud.test", transport=transport)
        registration = client.register_local(
            "one-use-binding-token",
            AgentIdentity(agent_id="agent-local-1", mode="local", version="0.2.0"),
            device_id="device-1",
        )

        self.assertEqual(registration["node_credential"], "node-secret")
        self.assertEqual(calls[0][1], "/api/v1/local-agent/nodes/register")
        self.assertEqual(calls[0][2]["binding_token"], "one-use-binding-token")
        self.assertEqual(calls[0][2]["device_id"], "device-1")
        self.assertNotIn("cookie", {key.lower() for key in calls[0][3]})

    def test_runtime_report_uses_bearer_node_credential(self) -> None:
        calls: list[tuple[str, str, Mapping[str, object], Mapping[str, str]]] = []

        def transport(method: str, path: str, payload: Mapping[str, object], headers: Mapping[str, str]) -> Mapping[str, object]:
            calls.append((method, path, payload, headers))
            return {"data": {"status": "reported"}}

        client = CloudAgentClient("http://cloud.test", transport=transport)
        report = {"operating_system": "macos", "bitbrowser_status": "unreachable"}

        client.report_runtime("node-1", "node-secret", report)

        self.assertEqual(calls[0][1], "/api/v1/local-agent/nodes/node-1/runtime-report")
        self.assertEqual(calls[0][2], report)
        self.assertEqual(calls[0][3], {"authorization": "Bearer node-secret"})

    def test_claim_task_sends_agent_and_lease(self) -> None:
        transport = RecordingTransport()
        client = CloudAgentClient("http://cloud.test", transport=transport)

        client.claim_task("agent-local-1", lease_seconds=30)

        self.assertEqual(transport.calls[0][0], "POST")
        self.assertEqual(transport.calls[0][1], "/api/v1/cloud-agent/tasks/claim")
        self.assertEqual(
            transport.calls[0][2],
            {"agent_id": "agent-local-1", "lease_seconds": 30},
        )

    def test_report_task_sends_status(self) -> None:
        transport = RecordingTransport()
        client = CloudAgentClient("http://cloud.test", transport=transport)

        client.report_task("task-1", "agent-local-1", "running", 50, "halfway")

        self.assertEqual(transport.calls[0][0], "POST")
        self.assertEqual(transport.calls[0][1], "/api/v1/cloud-agent/tasks/task-1/report")
        self.assertEqual(
            transport.calls[0][2],
            {
                "agent_id": "agent-local-1",
                "status": "running",
                "progress": 50,
                "message": "halfway",
            },
        )

    def test_missing_data_is_rejected(self) -> None:
        client = CloudAgentClient("http://cloud.test", transport=lambda method, path, payload, headers: {})

        with self.assertRaises(ValueError):
            client.heartbeat("agent-local-1")


if __name__ == "__main__":
    unittest.main()
