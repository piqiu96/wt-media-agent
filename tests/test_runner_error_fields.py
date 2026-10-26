"""CHG-057 T-05: the runner's failures carry `error_code` / `task_id` as fields.

`test_runtime_logging.py` proves the channel renders. This proves the *runner
actually uses it*: a real `TaskRunner`, a real SQLite store, and a real
`error.log` file, driven through the failure paths that exist today. A channel
nobody feeds would satisfy every rendering assertion on its own.

The doubles are minimal and deliberate: Cloud is a double (the point is the
Agent's own dispatch, as in the existing runner tests), the store is real, and
the executors are the smallest thing that can fail the two ways the runner
distinguishes.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from support import LoggingStateTestCase, isolated_paths

from wt_media_agent.clients.cloud import AgentAuthError, StaleAttemptError
from wt_media_agent.runtime.config import load_config
from wt_media_agent.runtime.logging import (
    AGENT_LOG_NAME,
    ERROR_LOG_NAME,
    configure_from,
)
from wt_media_agent.runner import TaskRunner, TaskRunnerConfig
from wt_media_agent.storage.checkpoint_store import CheckpointStore
from wt_media_agent.storage.migration import apply_migrations

RUNNER_LOGGER = "wt_media_agent.runner.runner"

TOKEN = "v1.payload.signature"
TASK = {
    "task_id": "t-1",
    "task_type": "demo_echo_task",
    "attempt_id": "attempt-1",
    "payload": "hello",
}


class Cloud:
    """Claims one task, then serves as the report sink."""

    def __init__(self, *, task=None, raise_on_claim=None, stale_progress=False) -> None:
        self.task = task
        self.raise_on_claim = raise_on_claim
        self.stale_progress = stale_progress
        self.reports: list[dict[str, object]] = []

    def claim_task(self, token: str, lease_seconds: int):
        if self.raise_on_claim is not None:
            raise self.raise_on_claim
        task, self.task = self.task, None
        return task

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
        if status == "running" and self.stale_progress:
            raise StaleAttemptError("STALE_ATTEMPT", "attempt superseded")
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


class FailingExecutor:
    def __init__(self, reporter, agent_id) -> None:
        pass

    def execute(self, task: Mapping[str, object]) -> None:
        raise RuntimeError("the executor blew up")


class StaleReportingExecutor:
    """Reports progress, which is the call that learns the attempt is gone."""

    def __init__(self, reporter, agent_id) -> None:
        self.reporter = reporter
        self.agent_id = agent_id

    def execute(self, task: Mapping[str, object]) -> None:
        self.reporter.report_task(task["task_id"], self.agent_id, "running", 10)


class RunnerFieldsTestCase(LoggingStateTestCase):
    """Runs the real runner against a real log directory."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)

        # The runner's own output directory is redirected too, so this test
        # cannot write into a checkout (CHG-057 T-02's rule).
        self.enterContext(isolated_paths())

        cfg = load_config(
            env={"WT_MEDIA_LOG_FILE": str(self.directory / AGENT_LOG_NAME)},
            frozen=False,
            repo_root=self.directory / "repo",
            home=self.directory / "home",
        )
        configure_from(cfg)

    def build(self, *, cloud: Cloud, register: str = "failing") -> TaskRunner:
        db_path = self.directory / "agent.db"
        apply_migrations(db_path)
        self.store = CheckpointStore(db_path)
        runner = TaskRunner(
            cloud,
            self.store,
            TaskRunnerConfig(
                agent_id="agent-7f3c",
                base_url="http://127.0.0.1:8188",
                db_path=str(db_path),
            ),
            token=lambda: TOKEN,
        )
        if register == "failing":
            runner.register_executor(TASK["task_type"], FailingExecutor)
        elif register == "stale":
            runner.register_executor(TASK["task_type"], StaleReportingExecutor)
        return runner

    def error_log(self) -> str:
        path = self.directory / ERROR_LOG_NAME
        return path.read_text() if path.is_file() else ""

    def test_a_failed_task_reaches_the_error_log_with_its_code_and_task_id(self):
        cloud = Cloud(task=dict(TASK))
        runner = self.build(cloud=cloud)

        runner._poll_once()

        written = self.error_log()
        self.assertIn("error_code=executor_error", written)
        self.assertIn("task_id=t-1", written)
        self.assertIn("the executor blew up", written)
        # The log line and the durable record tell one story: the terminal was
        # written locally, then the outbox delivered that same record.
        self.assertEqual(len(cloud.reports), 1)
        report = cloud.reports[0]
        self.assertEqual(report["attempt_id"], "attempt-1")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error_code"], "executor_error")
        self.assertIn("the executor blew up", str(report["error_message"]))
        self.assertIs(report["retryable"], True)
        self.assertEqual(report["recovery_action"], "retry")
        self.assertEqual(self.store.get_undelivered_results(), [])

    def test_a_superseded_attempt_is_logged_with_its_own_code_and_dropped(self):
        runner = self.build(
            cloud=Cloud(task=dict(TASK), stale_progress=True), register="stale"
        )

        with self.assertLogs(RUNNER_LOGGER, level="WARNING") as captured:
            runner._poll_once()

        records = [
            r for r in captured.records if "was superseded" in r.getMessage()
        ]
        self.assertEqual(len(records), 1, "positive control: the warning was emitted")
        self.assertEqual(records[0].error_code, "stale_attempt")
        self.assertEqual(records[0].task_id, "t-1")
        self.assertIsNone(self.store.get_checkpoint("t-1"))

    def test_a_missing_executor_is_a_warning_with_a_code_and_stays_out_of_error_log(self):
        """Two-way: the field is on the record, and the record is not an ERROR.

        `no executor` is a WARNING, so it belongs in `agent.log` only. Asserting
        the record (rather than the file) is what lets this check the field for
        a record `error.log` is supposed to refuse.
        """
        runner = self.build(cloud=Cloud(task=dict(TASK)), register="none")

        with self.assertLogs(RUNNER_LOGGER, level="WARNING") as captured:
            runner._poll_once()

        records = [r for r in captured.records if "no executor" in r.getMessage()]
        self.assertEqual(len(records), 1, "positive control: the warning was emitted")
        self.assertEqual(records[0].error_code, "no_executor")
        self.assertEqual(records[0].task_id, "t-1")

        # The negative needs a non-empty file to mean anything: an ERROR from
        # the same logger reaches error.log, and the WARNING's code does not --
        # even though the code string is one this file could have carried.
        logging.getLogger(RUNNER_LOGGER).error("a real error")
        written = self.error_log()
        self.assertIn("a real error", written)
        self.assertNotIn("no_executor", written)

    def test_a_failure_with_no_task_in_scope_still_renders(self):
        """A refused Agent Token happens before any task exists.

        That is the shape most records still have, so it must not raise -- and
        it renders as "no code", which is a fact about the event rather than a
        missing field.
        """
        runner = self.build(
            cloud=Cloud(
                raise_on_claim=AgentAuthError(
                    "AUTH_DESKTOP_REPLACED", "你的账号已在另一台电脑登录"
                )
            )
        )

        runner._poll_once()

        written = self.error_log()
        self.assertIn("Agent Token not accepted", written)
        self.assertIn("error_code=none", written)
        self.assertNotIn("task_id=", written)


if __name__ == "__main__":
    unittest.main()
