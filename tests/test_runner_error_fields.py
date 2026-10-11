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

from wt_media_agent.clients.cloud.client import SessionInvalidError
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

TASK = {"task_id": "t-1", "task_type": "demo_echo_task", "payload": "hello"}


class Cloud:
    """Claims one task, then serves as the report sink."""

    def __init__(self, *, task=None, raise_on_claim=None) -> None:
        self.task = task
        self.raise_on_claim = raise_on_claim
        self.reports: list[tuple[str, int, str]] = []

    def claim_task(self, agent_id: str, lease_seconds: int):
        if self.raise_on_claim is not None:
            raise self.raise_on_claim
        task, self.task = self.task, None
        return task

    def report_task(self, task_id, agent_id, status, progress, message):
        self.reports.append((status, progress, message))
        return {"ok": True}


class FailingExecutor:
    def __init__(self, client, agent_id) -> None:
        pass

    def execute(self, task: Mapping[str, object]) -> None:
        raise RuntimeError("the executor blew up")


class InvalidatingExecutor:
    def __init__(self, client, agent_id) -> None:
        pass

    def execute(self, task: Mapping[str, object]) -> None:
        raise SessionInvalidError("session revoked mid-task")


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
        )
        if register == "failing":
            runner.register_executor(TASK["task_type"], FailingExecutor)
        elif register == "invalidating":
            runner.register_executor(TASK["task_type"], InvalidatingExecutor)
        return runner

    def error_log(self) -> str:
        path = self.directory / ERROR_LOG_NAME
        return path.read_text() if path.is_file() else ""

    def test_a_failed_task_reaches_the_error_log_with_its_code_and_task_id(self):
        runner = self.build(cloud=Cloud(task=dict(TASK)))

        runner._poll_once()

        written = self.error_log()
        self.assertIn("error_code=executor_error", written)
        self.assertIn("task_id=t-1", written)
        self.assertIn("the executor blew up", written)
        # The log line and the durable record tell one story: the checkpoint
        # was written with the code the line names. (A generic executor failure
        # is *not* reported to Cloud here -- `_save_failed` only checkpoints it,
        # and queues an offline result once retries are exhausted.)
        checkpoint = self.store.get_checkpoint("t-1")
        self.assertEqual(checkpoint.checkpoint_status, "failed")
        self.assertEqual(checkpoint.error_code, "executor_error")
        self.assertIn("the executor blew up", checkpoint.message)
        self.assertEqual(runner.client.reports, [])

    def test_an_invalidated_session_is_reported_as_its_own_code(self):
        runner = self.build(cloud=Cloud(task=dict(TASK)), register="invalidating")

        runner._poll_once()

        written = self.error_log()
        self.assertIn("error_code=session_invalidated_result_uncertain", written)
        self.assertIn("task_id=t-1", written)

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
        """The runner's session-invalidated claim path has no task and no code.

        That is the shape most records still have today, so it must not raise --
        and it renders as "no code", which is a fact about the event rather than
        a missing field.
        """
        runner = self.build(cloud=Cloud(raise_on_claim=SessionInvalidError("revoked")))

        runner._poll_once()

        written = self.error_log()
        self.assertIn("agent session invalidated", written)
        self.assertIn("error_code=none", written)
        self.assertNotIn("task_id=", written)


if __name__ == "__main__":
    unittest.main()
