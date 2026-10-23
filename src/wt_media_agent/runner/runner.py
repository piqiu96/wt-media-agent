"""Agent TaskRunner：领取任务、执行、checkpoint，以及重启后的恢复。

结果的写入顺序是硬约束：先写 SQLite 终态与 outbox，再由 `OutboxDelivery`
上报 Cloud。每一轮先补报 outbox，未清空就不领取新任务。运行中的保活由这里
统一发 activity，执行器不各自起定时器。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Optional

from wt_media_agent.clients.cloud import AgentAuthError, CloudAgentClient, StaleAttemptError
from wt_media_agent.executors.protocol import ExecutorFactory
from wt_media_agent.runner.config import TaskRunnerConfig
from wt_media_agent.runner.reporter import DurableTaskReporter, OutboxDelivery
from wt_media_agent.storage.checkpoint_store import CheckpointStore

logger = logging.getLogger(__name__)

DEFAULT_EXECUTION_TIMEOUT_SECONDS = 300
ACTIVITY_MAX_SECONDS = 30.0
ACTIVITY_MIN_SECONDS = 5.0


class TaskRunner:
    """轮询 Cloud 任务并在本地执行，终态经 outbox 收敛。"""

    def __init__(
        self,
        client: CloudAgentClient,
        store: CheckpointStore,
        config: TaskRunnerConfig,
        *,
        token: Callable[[], Optional[str]],
        executors: Mapping[str, ExecutorFactory] | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.config = config
        # 读 Token 而不是持有 Token：Desktop 可以在进程运行期间换发新 Token。
        self._token = token
        self._outbox = OutboxDelivery(client, store, token)
        self._executors: dict[str, ExecutorFactory] = dict(executors or {})
        self._running = False
        self._refused_token: Optional[str] = None
        self._logged_missing_token = False

    def register_executor(self, task_type: str, factory: ExecutorFactory) -> None:
        self._executors[task_type] = factory

    # ---- Lifecycle ----

    def start(self) -> None:
        self._running = True
        self._recover()
        while self._running:
            try:
                self._poll_once()
            except Exception as exc:
                logger.error("runner poll error: %s", exc)
            time.sleep(self.config.poll_interval)

    def stop(self) -> None:
        self._running = False

    # ---- Recovery ----

    def _recover(self) -> None:
        """处理上次进程留下的非终态行。

        没有 `attempt_id` 的是旧模型的行，无法上报也无法重跑，直接删除。
        有外部副作用的行不能确定平台状态，写入 RESULT_UNKNOWN 终态；其余行
        保留，由 `_run_recovered_task` 重跑。
        """
        for cp in self.store.get_open_checkpoints():
            if not cp.attempt_id:
                logger.warning(
                    "dropping legacy checkpoint for task %s without attempt_id",
                    cp.task_id,
                )
                self.store.remove_checkpoint(cp.task_id)
                continue
            if cp.external_side_effect:
                logger.error(
                    "task %s had external side effects before restart; "
                    "recording RESULT_UNKNOWN",
                    cp.task_id,
                )
                self.store.finish_task(
                    task_id=cp.task_id,
                    agent_id=cp.agent_id,
                    attempt_id=cp.attempt_id,
                    status="failed",
                    progress=cp.progress,
                    message="Agent restarted while the task was running",
                    result_json=None,
                    error_code="RESULT_UNKNOWN",
                    error_message=(
                        "Agent restarted while the task was running; "
                        "check the platform state before retrying"
                    ),
                    retryable=False,
                    recovery_action="check_platform_state",
                )

    def _run_recovered_task(self) -> bool:
        for cp in self.store.get_open_checkpoints():
            payload = _decode_payload(cp.task_payload_json)
            logger.info(
                "resuming task %s at progress %d", cp.task_id, cp.progress
            )
            self._execute(
                task_id=cp.task_id,
                task_type=cp.task_type,
                attempt_id=cp.attempt_id or "",
                task_data=payload,
                execution_timeout_seconds=cp.execution_timeout_seconds
                or DEFAULT_EXECUTION_TIMEOUT_SECONDS,
                external_side_effect=bool(cp.external_side_effect),
            )
            return True
        return False

    # ---- Poll Loop ----

    def _poll_once(self) -> None:
        pending = self._outbox.flush()
        if self._run_recovered_task():
            return
        if pending:
            # 还有结果没送到 Cloud，先不领取新任务。
            return
        task_data = self._claim_task()
        if task_data is None:
            return
        task_id = str(task_data.get("task_id") or "")
        task_type = str(task_data.get("task_type") or "")
        attempt_id = str(task_data.get("attempt_id") or "")
        if not task_id or not task_type or not attempt_id:
            logger.error(
                "claim without task_id/task_type/attempt_id; ignored"
            )
            return
        timeout = _as_int(
            task_data.get("execution_timeout_seconds"),
            DEFAULT_EXECUTION_TIMEOUT_SECONDS,
        )
        self.store.start_task(
            task_id=task_id,
            task_type=task_type,
            agent_id=self.config.agent_id,
            attempt_id=attempt_id,
            payload_json=json.dumps(task_data, ensure_ascii=False, default=str),
            execution_timeout_seconds=timeout,
            external_side_effect=bool(task_data.get("external_side_effect")),
        )
        self._execute(
            task_id=task_id,
            task_type=task_type,
            attempt_id=attempt_id,
            task_data=task_data,
            execution_timeout_seconds=timeout,
            external_side_effect=bool(task_data.get("external_side_effect")),
        )

    def _claim_task(self) -> Optional[Mapping[str, object]]:
        token = self._token()
        if not token:
            if not self._logged_missing_token:
                logger.info("no Agent Token yet; not claiming")
                self._logged_missing_token = True
            return None
        self._logged_missing_token = False
        if token == self._refused_token:
            return None
        try:
            task = self.client.claim_task(token, self.config.lease_seconds)
            self._refused_token = None
            return task
        except AgentAuthError as exc:
            # 只停用这一个 Token；Desktop 换发新 Token 后下一轮自动恢复。
            self._refused_token = token
            logger.error(
                "Agent Token not accepted; not claiming until it is replaced: %s", exc
            )
            return None
        except Exception as exc:
            logger.debug("claim failed (may be normal): %s", exc)
            return None

    # ---- Execution ----

    def _execute(
        self,
        *,
        task_id: str,
        task_type: str,
        attempt_id: str,
        task_data: Mapping[str, object],
        execution_timeout_seconds: int,
        external_side_effect: bool,
    ) -> None:
        reporter = DurableTaskReporter(
            self.client,
            self.store,
            self._outbox,
            task_id=task_id,
            attempt_id=attempt_id,
            agent_id=self.config.agent_id,
        )
        factory = self._executors.get(task_type)
        if factory is None:
            logger.warning(
                "no executor for task type %s", task_type,
                extra={"task_id": task_id, "error_code": "no_executor"},
            )
            reporter.finish(
                status="failed",
                progress=0,
                message="no executor for this task type",
                error_code="no_executor",
                retryable=False,
                recovery_action="none",
            )
            return

        stop = self._start_activity(task_id, attempt_id, execution_timeout_seconds)
        try:
            self.store.record_progress(task_id, attempt_id, 0, "executing")
            logger.info("executing task %s (type=%s)", task_id, task_type,
                        extra={"task_id": task_id})
            returned = factory(reporter, self.config.agent_id).execute(task_data)
            if not reporter.finished:
                reporter.finish(
                    status="succeeded",
                    progress=100,
                    result=returned if isinstance(returned, Mapping) else None,
                )
            logger.info("task %s succeeded", task_id, extra={"task_id": task_id})
        except StaleAttemptError as exc:
            logger.warning(
                "task %s attempt %s was superseded; local result dropped",
                task_id, attempt_id,
                extra={"task_id": task_id, "error_code": "stale_attempt"},
            )
            self.store.remove_checkpoint(task_id)
            logger.debug("stale attempt detail: %s", exc)
        except Exception as exc:
            logger.error(
                "task %s failed: %s", task_id, exc,
                extra={"task_id": task_id, "error_code": "executor_error"},
            )
            if reporter.finished:
                logger.error(
                    "task %s failed after a terminal result was already recorded",
                    task_id,
                    extra={"task_id": task_id},
                )
            else:
                reporter.finish(
                    status="failed",
                    progress=0,
                    message=str(exc),
                    error_code="executor_error",
                    error_message=str(exc),
                    retryable=not external_side_effect,
                    recovery_action=(
                        "retry" if not external_side_effect else "check_platform_state"
                    ),
                )
        finally:
            stop.set()

    def _start_activity(
        self, task_id: str, attempt_id: str, execution_timeout_seconds: int
    ) -> threading.Event:
        """执行期间统一发 activity，避免长任务被 Cloud 超时扫描收敛。"""
        interval = max(
            ACTIVITY_MIN_SECONDS,
            min(ACTIVITY_MAX_SECONDS, execution_timeout_seconds / 3.0),
        )
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(interval):
                token = self._token()
                if not token:
                    continue
                try:
                    self.client.report_activity(
                        token, task_id, attempt_id, stage=self._current_stage(task_id)
                    )
                except AgentAuthError:
                    # Token 过期后由 Desktop 换发；执行本身不中断。
                    return
                except Exception as exc:
                    logger.debug("activity for task %s not delivered: %s", task_id, exc)

        threading.Thread(
            target=beat, name=f"agent-activity-{task_id}", daemon=True
        ).start()
        return stop

    def _current_stage(self, task_id: str) -> str:
        checkpoint = self.store.get_checkpoint(task_id)
        if checkpoint is None or not checkpoint.last_stage:
            return "executing"
        return checkpoint.last_stage


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _decode_payload(raw: Optional[str]) -> Mapping[str, object]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except ValueError:
        return {}
    return decoded if isinstance(decoded, Mapping) else {}
