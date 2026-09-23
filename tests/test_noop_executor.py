from __future__ import annotations

import unittest
from typing import Mapping

from wt_media_agent.clients.cloud import CloudAgentClient
from wt_media_agent.executors.noop import NoopExecutor


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Mapping[str, object]]] = []

    def __call__(self, method: str, path: str, payload: Mapping[str, object], headers: Mapping[str, str]) -> Mapping[str, object]:
        self.calls.append((method, path, payload))
        return {"data": {"task_id": "task-1", "status": payload["status"], "progress": payload["progress"]}}


class NoopExecutorTest(unittest.TestCase):
    def test_noop_executor_reports_lifecycle(self) -> None:
        transport = RecordingTransport()
        client = CloudAgentClient("http://cloud.test", transport=transport)
        executor = NoopExecutor(client, agent_id="agent-local-1")

        result = executor.execute({"task_id": "task-1", "task_type": "noop_task"})

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual([call[2]["status"] for call in transport.calls], ["running", "running", "succeeded"])
        self.assertEqual([call[2]["progress"] for call in transport.calls], [0, 50, 100])

    def test_noop_executor_rejects_wrong_task_type(self) -> None:
        executor = NoopExecutor(CloudAgentClient("http://cloud.test", transport=lambda m, p, body, headers: {}), "agent-local-1")

        with self.assertRaises(ValueError):
            executor.execute({"task_id": "task-1", "task_type": "other"})


if __name__ == "__main__":
    unittest.main()
