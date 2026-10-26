from __future__ import annotations

import unittest
from typing import Mapping, Optional

from wt_media_agent.executors.noop import NoopExecutor


class RecordingReporter:
    """执行器可见的上报通道；记录调用并按协议返回。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def report_task(
        self,
        task_id: str,
        agent_id: str,
        status: str,
        progress: int,
        message: str = "",
        result: Optional[Mapping[str, object]] = None,
    ) -> Mapping[str, object]:
        self.calls.append(
            {
                "task_id": task_id,
                "agent_id": agent_id,
                "status": status,
                "progress": progress,
                "message": message,
                "result": result,
            }
        )
        return {"task_id": task_id, "status": status, "progress": progress}


class NoopExecutorTest(unittest.TestCase):
    def test_noop_executor_reports_lifecycle(self) -> None:
        reporter = RecordingReporter()
        executor = NoopExecutor(reporter, "agent-local-1")

        result = executor.execute({"task_id": "task-1", "task_type": "noop_task"})

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(
            [call["status"] for call in reporter.calls], ["running", "running", "succeeded"]
        )
        self.assertEqual([call["progress"] for call in reporter.calls], [0, 50, 100])

    def test_noop_executor_rejects_wrong_task_type(self) -> None:
        executor = NoopExecutor(RecordingReporter(), "agent-local-1")

        with self.assertRaises(ValueError):
            executor.execute({"task_id": "task-1", "task_type": "other"})


if __name__ == "__main__":
    unittest.main()
