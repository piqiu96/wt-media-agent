"""Agent TaskRunner with SQLite checkpoints, offline queue, and restart recovery."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Optional

from wt_media_agent.cloud_agent_client import CloudAgentClient
from wt_media_agent.constants import (
    DEFAULT_LEASE_SECONDS,
    MAX_RETRIES,
    TASK_TYPE_ACCOUNT_CHECK,
    TASK_TYPE_COOKIE_READ,
    TASK_TYPE_COOKIE_WRITE,
    TASK_TYPE_NOOP,
    TASK_TYPE_PROXY_CHECK,
)
from wt_media_agent.executors.noop import NoopExecutor
from wt_media_agent.executors.cookie import CookieReadExecutor, CookieWriteExecutor
from wt_media_agent.executors.account_check import AccountCheckExecutor
from wt_media_agent.storage.checkpoint_store import (
    CheckpointStore,
    OfflineResult,
    TaskCheckpoint,
)

logger = logging.getLogger(__name__)

ExecutorFactory = Callable[[CloudAgentClient, str], object]


@dataclass
class TaskRunnerConfig:
    agent_id: str
    base_url: str
    db_path: str
    poll_interval: float = 5.0
    lease_seconds: int = 60
    max_retries: int = 3


class TaskRunner:
    """Polls Cloud for tasks, executes them with checkpoints, and recovers on restart."""

    def __init__(
        self,
        client: CloudAgentClient,
        store: CheckpointStore,
        config: TaskRunnerConfig,
    ) -> None:
        self.client = client
        self.store = store
        self.config = config
        self._running = False
        self._executors: dict[str, ExecutorFactory] = {
            TASK_TYPE_NOOP: lambda c, a: NoopExecutor(c, a),
            TASK_TYPE_COOKIE_READ: lambda c, a: CookieReadExecutor(c, a),
            TASK_TYPE_COOKIE_WRITE: lambda c, a: CookieWriteExecutor(c, a),
            TASK_TYPE_ACCOUNT_CHECK: lambda c, a: AccountCheckExecutor(c, a),
        }

    def register_executor(self, task_type: str, factory: ExecutorFactory) -> None:
        self._executors[task_type] = factory

    # ---- Lifecycle ----

    def start(self) -> None:
        """Start the runner loop. Blocks until stopped."""
        self._running = True
        self._recover_incomplete_tasks()
        self._flush_offline_queue()
        while self._running:
            try:
                self._poll_once()
            except Exception as exc:
                logger.error("runner poll error: %s", exc)
            time.sleep(self.config.poll_interval)

    def stop(self) -> None:
        self._running = False

    # ---- Recovery ----

    def _recover_incomplete_tasks(self) -> None:
        """Re-discover tasks that were in progress before a restart."""
        incomplete = self.store.get_incomplete_checkpoints()
        if not incomplete:
            return
        logger.info("recovering %d incomplete task(s)", len(incomplete))
        for cp in incomplete:
            if cp.checkpoint_status == "claimed":
                logger.info("re-claiming task %s (previous claim)", cp.task_id)
            elif cp.checkpoint_status == "running":
                logger.info("resuming task %s at progress %d", cp.task_id, cp.progress)

    def _flush_offline_queue(self) -> None:
        """Deliver any results queued while Cloud was unreachable."""
        undelivered = self.store.get_undelivered_results()
        if not undelivered:
            return
        logger.info("flushing %d offline result(s)", len(undelivered))
        for result in undelivered:
            try:
                self.client.report_task(
                    result.task_id, result.agent_id,
                    result.status, result.progress, result.message or "",
                )
                self.store.mark_delivered(result.id)
                logger.info("delivered offline result for task %s", result.task_id)
            except Exception as exc:
                logger.warning("offline delivery failed for task %s: %s", result.task_id, exc)

    # ---- Poll Loop ----

    def _poll_once(self) -> None:
        task_data = self._claim_task()
        if task_data is None:
            return
        task_id = str(task_data.get("task_id", ""))
        task_type = str(task_data.get("task_type", ""))
        if not task_id:
            return

        self._save_claimed(task_id, task_type)
        executor = self._executors.get(task_type)
        if executor is None:
            logger.warning("no executor for task type %s", task_type)
            self._report_failed(task_id, "no_executor")
            return

        try:
            logger.info("executing task %s (type=%s)", task_id, task_type)
            self._save_running(task_id, task_type, progress=0, message="executing")
            instance = executor(self.client, self.config.agent_id)
            if hasattr(instance, "execute"):
                instance.execute(task_data)
            self._save_completed(task_id, task_type)
            self.store.remove_checkpoint(task_id)
            logger.info("task %s succeeded", task_id)
        except Exception as exc:
            logger.error("task %s failed: %s", task_id, exc)
            self._save_failed(task_id, task_type, str(exc))

    def _claim_task(self) -> Optional[Mapping[str, object]]:
        try:
            return self.client.claim_task(self.config.agent_id, self.config.lease_seconds)
        except Exception as exc:
            logger.debug("claim failed (may be normal): %s", exc)
            return None

    # ---- Checkpoint helpers ----

    def _save_claimed(self, task_id: str, task_type: str) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.store.save_checkpoint(TaskCheckpoint(
            task_id=task_id,
            task_type=task_type,
            agent_id=self.config.agent_id,
            checkpoint_status="claimed",
            created_at=now,
            updated_at=now,
        ))

    def _save_running(self, task_id: str, task_type: str, progress: int, message: str) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cp = self.store.get_checkpoint(task_id) or TaskCheckpoint(
            task_id=task_id, task_type=task_type, agent_id=self.config.agent_id,
            checkpoint_status="", created_at=now, updated_at=now,
        )
        cp.checkpoint_status = "running"
        cp.progress = progress
        cp.message = message
        cp.updated_at = now
        self.store.save_checkpoint(cp)

    def _save_completed(self, task_id: str, task_type: str) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cp = self.store.get_checkpoint(task_id) or TaskCheckpoint(
            task_id=task_id, task_type=task_type, agent_id=self.config.agent_id,
            checkpoint_status="", created_at=now, updated_at=now,
        )
        cp.checkpoint_status = "completed"
        cp.progress = 100
        cp.updated_at = now
        self.store.save_checkpoint(cp)

    def _save_failed(self, task_id: str, task_type: str, error: str) -> None:
        cp = self.store.get_checkpoint(task_id) or TaskCheckpoint(
            task_id=task_id, task_type=task_type, agent_id=self.config.agent_id,
            checkpoint_status="", updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        cp.checkpoint_status = "failed"
        cp.error_code = "executor_error"
        cp.message = error
        cp.retry_count += 1
        if cp.retry_count >= self.config.max_retries:
            self._queue_offline_result(task_id, "failed", 0, error)
        self.store.save_checkpoint(cp)

    def _report_failed(self, task_id: str, error_code: str) -> None:
        try:
            self.client.report_task(task_id, self.config.agent_id, "failed", 0, error_code)
        except Exception:
            self._queue_offline_result(task_id, "failed", 0, error_code)

    def _queue_offline_result(self, task_id: str, status: str, progress: int, message: str) -> None:
        self.store.enqueue_result(OfflineResult(
            task_id=task_id, agent_id=self.config.agent_id,
            status=status, progress=progress, message=message,
        ))
