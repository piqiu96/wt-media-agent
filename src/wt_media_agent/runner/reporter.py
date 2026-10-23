"""执行器使用的上报通道，以及终态结果的持久化 outbox。

执行器只有 `report_task` 这一个接口。终态由这里先写入 SQLite（checkpoint +
outbox 同一事务），再尝试上报 Cloud；Cloud 不可达时结果不会丢，运行中的进度
上报失败则只记日志，不影响继续执行。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import Optional

from wt_media_agent.clients.cloud import (
    CloudAgentClient,
    CloudError,
    CloudUnavailableError,
    StaleAttemptError,
)
from wt_media_agent.storage.checkpoint_store import CheckpointStore

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")


class OutboxDelivery:
    """把未投递的终态结果补报给 Cloud。"""

    def __init__(
        self,
        client: CloudAgentClient,
        store: CheckpointStore,
        token: Callable[[], Optional[str]],
    ) -> None:
        self.client = client
        self.store = store
        self._token = token

    def current_token(self) -> Optional[str]:
        return self._token()

    def flush(self) -> int:
        """补报所有待投递结果，返回仍未投递的数量。"""
        pending = self.store.get_undelivered_results()
        if not pending:
            return 0
        token = self._token()
        if not token:
            return len(pending)
        for result in pending:
            if not result.attempt_id:
                # 没有 attempt_id 的旧行无法通过上报档校验，只能丢弃。
                logger.warning(
                    "dropping offline result without attempt_id for task %s",
                    result.task_id,
                )
                self.store.drop_result(result.id)
                continue
            try:
                self.client.report_task(
                    token,
                    result.task_id,
                    result.attempt_id,
                    status=result.status,
                    progress=result.progress,
                    message=result.message or "",
                    error_code=result.error_code,
                    error_message=result.error_message,
                    retryable=bool(result.retryable),
                    recovery_action=result.recovery_action,
                    result=_decode_result(result.result_json),
                )
            except StaleAttemptError:
                # Cloud 已启动新 attempt，旧结果不能覆盖。
                self.store.drop_result(result.id)
                logger.warning(
                    "offline result for task %s belongs to a superseded attempt; dropped",
                    result.task_id,
                )
            except Exception as exc:  # noqa: BLE001 - 任何失败都保留 outbox
                self.store.record_delivery_failure(result.id)
                logger.warning(
                    "offline delivery failed for task %s: %s", result.task_id, exc
                )
                break
            else:
                self.store.mark_delivered(result.id)
                logger.info(
                    "delivered offline result for task %s", result.task_id
                )
        return len(self.store.get_undelivered_results())


class DurableTaskReporter:
    """执行器看到的“client”：进度即时上报，终态先落盘再上报。"""

    def __init__(
        self,
        client: CloudAgentClient,
        store: CheckpointStore,
        outbox: OutboxDelivery,
        *,
        task_id: str,
        attempt_id: str,
        agent_id: str,
    ) -> None:
        self.client = client
        self.store = store
        self.outbox = outbox
        self.task_id = task_id
        self.attempt_id = attempt_id
        self.agent_id = agent_id
        # 终态已写过之后 checkpoint 可能被清理，Runner 不能再用它判断“是否已终结”。
        self.finished = False

    def report_task(
        self,
        task_id: str,
        agent_id: str,
        status: str,
        progress: int,
        message: str = "",
        result: Optional[Mapping[str, object]] = None,
    ) -> Mapping[str, object]:
        if task_id != self.task_id:
            raise ValueError(
                f"executor reported for {task_id!r} while running {self.task_id!r}"
            )
        if status in TERMINAL_STATUSES:
            return self.finish(
                status=status, progress=progress, message=message, result=result
            )
        self._report_progress(progress, message)
        return {"task_id": task_id, "status": status, "progress": int(progress)}

    def _report_progress(self, progress: int, message: str) -> None:
        self.store.record_progress(
            self.task_id, self.attempt_id, int(progress), message
        )
        token = self.outbox.current_token()
        if not token:
            return
        try:
            self.client.report_task(
                token,
                self.task_id,
                self.attempt_id,
                status="running",
                progress=int(progress),
                message=message,
            )
        except StaleAttemptError:
            # 任务已被重试，继续执行只会产出不会有人接收的结果。
            raise
        except (CloudError, CloudUnavailableError) as exc:
            logger.debug(
                "progress report for task %s not delivered: %s", self.task_id, exc
            )

    def finish(
        self,
        *,
        status: str,
        progress: int = 0,
        message: str = "",
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        retryable: Optional[bool] = None,
        recovery_action: Optional[str] = None,
        result: Optional[Mapping[str, object]] = None,
    ) -> Mapping[str, object]:
        self.store.finish_task(
            task_id=self.task_id,
            agent_id=self.agent_id,
            attempt_id=self.attempt_id,
            status=status,
            progress=int(progress),
            message=message,
            result_json=_encode_result(result),
            error_code=error_code,
            error_message=error_message,
            retryable=bool(retryable),
            recovery_action=recovery_action,
        )
        self.finished = True
        self.outbox.flush()
        return {
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "status": status,
            "progress": int(progress),
        }


def _encode_result(result: Optional[Mapping[str, object]]) -> Optional[str]:
    if not isinstance(result, Mapping):
        return None
    return json.dumps(dict(result), ensure_ascii=False, separators=(",", ":"))


def _decode_result(raw: Optional[str]) -> Optional[Mapping[str, object]]:
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
    except ValueError:
        return None
    return decoded if isinstance(decoded, Mapping) else None
