"""TaskRunner 对 Agent Token 的两种状态：没有和已被拒绝。

两种情况都只影响领取：loop 本身、outbox 补报和运行中的任务都不受影响；Desktop
换发新 Token 后下一轮自动恢复。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wt_media_agent.clients.cloud import AgentAuthError
from wt_media_agent.runner import TaskRunner, TaskRunnerConfig

TOKEN = "v1.first.signature"
REPLACED = "v1.second.signature"


class RefusingClient:
    def __init__(self) -> None:
        self.tokens: list[str] = []

    def claim_task(self, token: str, lease_seconds: int):
        self.tokens.append(token)
        raise AgentAuthError("AUTH_DESKTOP_REPLACED", "你的账号已在另一台电脑登录")


class RefuseThenAnswer(RefusingClient):
    def claim_task(self, token: str, lease_seconds: int):
        self.tokens.append(token)
        if len(self.tokens) == 1:
            raise AgentAuthError("AUTH_DESKTOP_REPLACED", "你的账号已在另一台电脑登录")
        return {"task_id": "task-1", "task_type": "noop", "attempt_id": "attempt-1"}


class RunnerTokenTest(unittest.TestCase):
    def runner(self, client, token) -> TaskRunner:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return TaskRunner(
            client,
            object(),
            TaskRunnerConfig(
                agent_id="agent-1",
                base_url="http://cloud.test",
                db_path=Path(tmp.name) / "agent.db",
            ),
            token=lambda: token[0],
        )

    def test_a_refused_token_is_not_asked_about_again(self) -> None:
        client = RefusingClient()
        holder = [TOKEN]
        runner = self.runner(client, holder)
        runner._running = True

        self.assertIsNone(runner._claim_task())
        self.assertIsNone(runner._claim_task())

        self.assertEqual(client.tokens, [TOKEN])
        self.assertTrue(runner._running)

    def test_a_new_token_is_tried_at_the_next_poll(self) -> None:
        client = RefuseThenAnswer()
        holder = [TOKEN]
        runner = self.runner(client, holder)
        runner._running = True
        runner._claim_task()

        holder[0] = REPLACED
        task = runner._claim_task()

        self.assertEqual(client.tokens, [TOKEN, REPLACED])
        self.assertEqual(task["task_id"], "task-1")  # type: ignore[index]

    def test_no_token_claims_nothing_and_logs_once(self) -> None:
        client = RefusingClient()
        runner = self.runner(client, [None])

        with self.assertLogs("wt_media_agent.runner.runner", level="INFO") as logs:
            runner._claim_task()
            runner._claim_task()

        self.assertEqual(client.tokens, [])
        self.assertEqual(len(logs.records), 1)


if __name__ == "__main__":
    unittest.main()
