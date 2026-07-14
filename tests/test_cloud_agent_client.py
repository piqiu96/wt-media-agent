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
        self.calls: list[tuple[str, str, Mapping[str, object]]] = []

    def __call__(self, method: str, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        self.calls.append((method, path, payload))
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

    def test_missing_data_is_rejected(self) -> None:
        client = CloudAgentClient("http://cloud.test", transport=lambda method, path, payload: {})

        with self.assertRaises(ValueError):
            client.heartbeat("agent-local-1")


if __name__ == "__main__":
    unittest.main()
