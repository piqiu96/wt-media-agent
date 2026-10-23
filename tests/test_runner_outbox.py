"""Runner 的结果 outbox：先落盘、先补报，再领取和执行。

这些路径在进程崩溃、Cloud 不可达或 attempt 被取代时才会走到，单看执行成功
的用例永远看不到它们。每个用例都直接读真实 SQLite 里的行，而不是相信
Cloud 替身收到的调用。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Mapping
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wt_media_agent.clients.cloud import (
    AgentAuthError,
    CloudUnavailableError,
    StaleAttemptError,
)
from wt_media_agent.runner import TaskRunner, TaskRunnerConfig
from wt_media_agent.runner import runner as runner_module
from wt_media_agent.storage.checkpoint_store import CheckpointStore
from wt_media_agent.storage.migration import apply_migrations

TOKEN = "v1.payload.signature"
TASK = {
    "task_id": "task-1",
    "task_type": "demo_task",
    "attempt_id": "attempt-1",
    "payload": "hello",
}


class RecordingCloud:
    """记录调用顺序；失败开关让每个用例只制造一种故障。"""

    def __init__(
        self,
        tasks=(),
        *,
        fail_report=False,
        stale_report=False,
        refuse_claim=False,
        store: CheckpointStore | None = None,
    ) -> None:
        self.tasks = list(tasks)
        self.fail_report = fail_report
        self.stale_report = stale_report
        self.refuse_claim = refuse_claim
        self.store = store
        self.calls: list[str] = []
        self.claims: list[str] = []
        self.reports: list[dict[str, object]] = []
        self.activities: list[tuple[str, str, str]] = []
        #: 每次上报时 outbox 里是否已经有这一行 —— 证明写盘发生在调用 Cloud 之前。
        self.undelivered_at_report: list[bool] = []

    def claim_task(self, token: str, lease_seconds: int):
        self.calls.append("claim")
        self.claims.append(token)
        if self.refuse_claim:
            raise AgentAuthError("AUTH_DESKTOP_REPLACED", "你的账号已在另一台电脑登录")
        return self.tasks.pop(0) if self.tasks else None

    def report_task(
        self,
        token: str,
        task_id: str,
        attempt_id: str,
        *,
        status: str,
        progress: int = 0,
        message: str = "",
        error_code=None,
        error_message=None,
        retryable=None,
        recovery_action=None,
        result=None,
    ) -> Mapping[str, object]:
        self.calls.append("report")
        if self.store is not None:
            self.undelivered_at_report.append(bool(self.store.get_undelivered_results()))
        if self.stale_report:
            raise StaleAttemptError("STALE_ATTEMPT", "attempt superseded")
        if self.fail_report:
            raise CloudUnavailableError("cloud could not be reached")
        self.reports.append(
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "status": status,
                "progress": progress,
                "message": message,
                "error_code": error_code,
                "error_message": error_message,
                "retryable": retryable,
                "recovery_action": recovery_action,
                "result": result,
            }
        )
        return {"task_id": task_id, "status": status}

    def report_activity(
        self, token: str, task_id: str, attempt_id: str, *, message="", stage=""
    ) -> None:
        self.activities.append((task_id, attempt_id, stage))


class FailingExecutor:
    def __init__(self, reporter, agent_id) -> None:
        pass

    def execute(self, task: Mapping[str, object]) -> None:
        raise RuntimeError("the executor blew up")


class SucceedingExecutor:
    def __init__(self, reporter, agent_id) -> None:
        self.reporter = reporter
        self.agent_id = agent_id
        self.tasks: list[Mapping[str, object]] = []

    def execute(self, task: Mapping[str, object]) -> Mapping[str, object]:
        self.tasks.append(task)
        return {"answer": "done"}


class SleepingExecutor(SucceedingExecutor):
    def execute(self, task: Mapping[str, object]) -> Mapping[str, object]:
        time.sleep(0.12)
        return super().execute(task)


class RunnerOutboxTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "agent.db"
        apply_migrations(self.db_path)
        self.store = CheckpointStore(self.db_path)

    def runner(self, cloud: RecordingCloud, executors=None) -> TaskRunner:
        return TaskRunner(
            cloud,
            self.store,
            TaskRunnerConfig(
                agent_id="agent-1",
                base_url="http://cloud.test",
                db_path=str(self.db_path),
            ),
            token=lambda: TOKEN,
            executors=executors,
        )

    def seed_result(self, *, task_id="task-1", attempt_id="attempt-1") -> None:
        """一条上次进程写下、还没送达 Cloud 的终态。"""
        self.store.start_task(
            task_id=task_id,
            task_type="demo_task",
            agent_id="agent-1",
            attempt_id=attempt_id,
            payload_json=json.dumps(TASK),
            execution_timeout_seconds=300,
            external_side_effect=False,
        )
        self.store.finish_task(
            task_id=task_id,
            agent_id="agent-1",
            attempt_id=attempt_id,
            status="succeeded",
            progress=100,
            message="done",
            result_json=json.dumps({"account_id": "acc-1"}),
            error_code=None,
            error_message=None,
            retryable=False,
            recovery_action=None,
        )

    # ---- 终态先落盘 ----

    def test_the_terminal_is_in_sqlite_before_cloud_is_told(self) -> None:
        cloud = RecordingCloud([dict(TASK)], fail_report=True, store=self.store)
        runner = self.runner(cloud, {"demo_task": lambda r, a: FailingExecutor(r, a)})

        runner._poll_once()

        self.assertEqual(cloud.undelivered_at_report, [True], "上报时结果已经在 SQLite 里")
        pending = self.store.get_undelivered_results()
        self.assertEqual(len(pending), 1)
        row = pending[0]
        self.assertEqual(row.status, "failed")
        self.assertEqual(row.attempt_id, "attempt-1")
        self.assertEqual(row.error_code, "executor_error")
        self.assertEqual(row.retryable, 1)
        self.assertEqual(row.recovery_action, "retry")
        self.assertIn("the executor blew up", str(row.error_message))
        checkpoint = self.store.get_checkpoint("task-1")
        self.assertEqual(checkpoint.final_status, "failed")
        self.assertEqual(checkpoint.report_pending, 1)
        self.assertEqual(cloud.reports, [], "Cloud 拒绝了这次上报")

        cloud.fail_report = False
        runner._poll_once()

        self.assertEqual([r["status"] for r in cloud.reports], ["failed"])
        self.assertEqual(cloud.reports[0]["attempt_id"], "attempt-1")
        self.assertEqual(self.store.get_undelivered_results(), [])
        self.assertIsNone(self.store.get_checkpoint("task-1"))

    def test_a_delivered_result_is_not_reported_again(self) -> None:
        cloud = RecordingCloud([dict(TASK)])
        runner = self.runner(cloud, {"demo_task": lambda r, a: SucceedingExecutor(r, a)})

        runner._poll_once()
        runner._poll_once()

        self.assertEqual([r["status"] for r in cloud.reports], ["succeeded"])
        self.assertEqual(self.store.get_undelivered_results(), [])

    # ---- 启动先补报 ----

    def test_a_restart_delivers_the_outbox_before_it_claims(self) -> None:
        self.seed_result()
        cloud = RecordingCloud(fail_report=True)
        runner = self.runner(cloud)

        runner._poll_once()

        self.assertEqual(cloud.calls.count("claim"), 0, "outbox 未清空时不得领取")
        self.assertNotEqual(self.store.get_undelivered_results(), [])

        cloud.fail_report = False
        cloud.calls.clear()
        runner._poll_once()

        self.assertEqual(cloud.calls[:2], ["report", "claim"])
        self.assertEqual([r["task_id"] for r in cloud.reports], ["task-1"])
        self.assertEqual(self.store.get_undelivered_results(), [])

    def test_a_refused_claim_does_not_block_the_outbox(self) -> None:
        self.seed_result()
        cloud = RecordingCloud(refuse_claim=True)
        runner = self.runner(cloud)

        runner._poll_once()

        self.assertEqual(cloud.calls, ["report", "claim"])
        self.assertEqual([r["status"] for r in cloud.reports], ["succeeded"])

    def test_a_superseded_attempt_drops_its_result_instead_of_retrying(self) -> None:
        self.seed_result()
        cloud = RecordingCloud(stale_report=True)
        runner = self.runner(cloud)

        runner._poll_once()
        runner._poll_once()

        self.assertEqual(cloud.reports, [])
        self.assertEqual(cloud.calls.count("report"), 1, "被取代的结果不再重试")
        self.assertEqual(self.store.get_undelivered_results(), [])
        self.assertIsNone(self.store.get_checkpoint("task-1"))

    # ---- 重启后的非终态行 ----

    def test_a_task_without_side_effects_is_run_again_on_the_same_attempt(self) -> None:
        self.store.start_task(
            task_id="task-1",
            task_type="demo_task",
            agent_id="agent-1",
            attempt_id="attempt-1",
            payload_json=json.dumps(TASK),
            execution_timeout_seconds=300,
            external_side_effect=False,
        )
        self.store.record_progress("task-1", "attempt-1", 25, "cookies")
        cloud = RecordingCloud()
        executor = SucceedingExecutor(None, None)
        runner = self.runner(cloud, {"demo_task": lambda r, a: executor})

        runner._poll_once()

        self.assertEqual(cloud.claims, [], "恢复不是一次新的领取")
        self.assertEqual(executor.tasks[0]["payload"], "hello")
        self.assertEqual([r["status"] for r in cloud.reports], ["succeeded"])
        self.assertEqual(cloud.reports[0]["attempt_id"], "attempt-1")

    def test_a_task_with_side_effects_is_not_run_again_after_a_restart(self) -> None:
        self.store.start_task(
            task_id="task-1",
            task_type="demo_task",
            agent_id="agent-1",
            attempt_id="attempt-1",
            payload_json=json.dumps(TASK),
            execution_timeout_seconds=300,
            external_side_effect=True,
        )
        self.store.record_progress("task-1", "attempt-1", 40, "published")
        cloud = RecordingCloud()
        executor = SucceedingExecutor(None, None)
        runner = self.runner(cloud, {"demo_task": lambda r, a: executor})

        runner._recover()
        runner._poll_once()

        self.assertEqual(executor.tasks, [], "有外部副作用的任务不得重跑")
        self.assertEqual(len(cloud.reports), 1)
        report = cloud.reports[0]
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error_code"], "RESULT_UNKNOWN")
        self.assertIs(report["retryable"], False)
        self.assertEqual(report["recovery_action"], "check_platform_state")

    def test_a_legacy_checkpoint_without_an_attempt_is_dropped(self) -> None:
        """旧模型的行没有 attempt_id，既无法上报也不能安全重跑。"""
        self.store.start_task(
            task_id="legacy-1",
            task_type="demo_task",
            agent_id="agent-1",
            attempt_id="",
            payload_json=json.dumps(TASK),
            execution_timeout_seconds=300,
            external_side_effect=False,
        )
        cloud = RecordingCloud()
        executor = SucceedingExecutor(None, None)
        runner = self.runner(cloud, {"demo_task": lambda r, a: executor})

        runner._recover()
        runner._poll_once()

        self.assertEqual(executor.tasks, [])
        self.assertIsNone(self.store.get_checkpoint("legacy-1"))

    # ---- 运行中的活动上报 ----

    def test_the_runner_refreshes_activity_while_a_task_runs(self) -> None:
        cloud = RecordingCloud([dict(TASK)])
        runner = self.runner(cloud, {"demo_task": lambda r, a: SleepingExecutor(r, a)})

        with mock.patch.object(runner_module, "ACTIVITY_MIN_SECONDS", 0.01), \
                mock.patch.object(runner_module, "ACTIVITY_MAX_SECONDS", 0.01):
            runner._poll_once()

        self.assertGreaterEqual(len(cloud.activities), 1)
        task_id, attempt_id, stage = cloud.activities[0]
        self.assertEqual((task_id, attempt_id), ("task-1", "attempt-1"))
        self.assertEqual(stage, "executing")


if __name__ == "__main__":
    unittest.main()
