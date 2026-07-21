from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wt_media_agent.cloud_agent_client import SessionInvalidError
from wt_media_agent.runner import TaskRunner, TaskRunnerConfig


class InvalidSessionClient:
    def claim_task(self, agent_id: str, lease_seconds: int):
        raise SessionInvalidError("session invalid")


class RunnerSessionTest(unittest.TestCase):
    def test_invalid_session_stops_claim_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = TaskRunner(
                InvalidSessionClient(),
                object(),
                TaskRunnerConfig(agent_id="agent-1", base_url="http://cloud.test", db_path=Path(tmp) / "agent.db"),
            )
            runner._running = True

            self.assertIsNone(runner._claim_task())
            self.assertFalse(runner._running)


if __name__ == "__main__":
    unittest.main()
