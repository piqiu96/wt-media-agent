"""SQLite 中的任务 checkpoint 与结果 outbox。

终态先和 outbox 行在同一事务里落盘，之后才允许上报 Cloud；上报失败的记录
留在 outbox，重启后先补报再领取新任务。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from wt_media_agent.storage.sqlite import connect
from wt_media_agent.utils.time import utc_now_iso


@dataclass
class TaskCheckpoint:
    task_id: str
    task_type: str
    agent_id: str
    checkpoint_status: str  # claimed, running, completed, failed
    progress: int = 0
    message: Optional[str] = None
    result_json: Optional[str] = None
    error_code: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    retry_count: int = 0
    #: A `TransferResume` in its encoded form (migration `0003_transfer_resume`).
    transfer_state_json: Optional[str] = None
    #: Migration `0004_task_recovery_outbox`。字段必须与 `SELECT *` 的列一一对应：
    #: `get_checkpoint` 用 `dict(row)` 构造本类，多一列少一列都会在读取时 TypeError。
    attempt_id: Optional[str] = None
    task_payload_json: Optional[str] = None
    last_stage: Optional[str] = None
    checkpoint_json: Optional[str] = None
    final_status: Optional[str] = None
    final_result_json: Optional[str] = None
    error_message: Optional[str] = None
    report_pending: int = 0
    last_report_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    execution_timeout_seconds: int = 300
    external_side_effect: int = 0


@dataclass
class OfflineResult:
    id: int = 0
    task_id: str = ""
    agent_id: str = ""
    status: str = ""
    progress: int = 0
    message: Optional[str] = None
    error_code: Optional[str] = None
    queued_at: str = ""
    delivered: bool = False
    attempt_id: Optional[str] = None
    result_json: Optional[str] = None
    error_message: Optional[str] = None
    retryable: int = 0
    recovery_action: Optional[str] = None
    delivery_attempts: int = 0
    delivered_at: Optional[str] = None


_CHECKPOINT_COLUMNS = (
    "task_id", "task_type", "agent_id", "checkpoint_status", "progress",
    "message", "result_json", "error_code", "created_at", "updated_at",
    "retry_count", "transfer_state_json", "attempt_id", "task_payload_json",
    "last_stage", "checkpoint_json", "final_status", "final_result_json",
    "error_message", "report_pending", "last_report_at", "started_at",
    "finished_at", "execution_timeout_seconds", "external_side_effect",
)


class CheckpointStore:
    """Persists task checkpoints and the undelivered terminal results."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        return connect(self._db_path)

    def probe(self) -> None:
        """Read one row and nothing else, to answer "is storage usable here?".

        It reads `task_checkpoints` rather than `SELECT 1`: a database that never
        migrated would otherwise be reported healthy. It never creates the file,
        and it closes its connection because a health poll may run every few
        seconds.
        """
        if not self._db_path.exists():
            raise FileNotFoundError(f"no database at {self._db_path}")
        connection = self._connect()
        try:
            with connection:
                connection.execute("SELECT 1 FROM task_checkpoints LIMIT 1").fetchone()
        finally:
            connection.close()

    # ---- Task Checkpoints ----

    def save_checkpoint(self, cp: TaskCheckpoint) -> None:
        now = utc_now_iso()
        values = (
            cp.task_id, cp.task_type, cp.agent_id, cp.checkpoint_status,
            cp.progress, cp.message, cp.result_json, cp.error_code,
            cp.created_at or now, now, cp.retry_count, cp.transfer_state_json,
            cp.attempt_id, cp.task_payload_json, cp.last_stage, cp.checkpoint_json,
            cp.final_status, cp.final_result_json, cp.error_message,
            cp.report_pending, cp.last_report_at, cp.started_at, cp.finished_at,
            cp.execution_timeout_seconds, cp.external_side_effect,
        )
        placeholders = ", ".join("?" for _ in _CHECKPOINT_COLUMNS)
        with self._connect() as db:
            db.execute(
                f"""INSERT OR REPLACE INTO task_checkpoints
                    ({', '.join(_CHECKPOINT_COLUMNS)})
                    VALUES ({placeholders})""",
                values,
            )

    def get_checkpoint(self, task_id: str) -> Optional[TaskCheckpoint]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM task_checkpoints WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        return TaskCheckpoint(**dict(row))

    def get_open_checkpoints(self) -> list[TaskCheckpoint]:
        """本次进程还需要执行或恢复的任务行。"""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM task_checkpoints "
                "WHERE checkpoint_status IN ('claimed', 'running') ORDER BY updated_at ASC"
            ).fetchall()
        return [TaskCheckpoint(**dict(row)) for row in rows]

    def get_incomplete_checkpoints(self) -> list[TaskCheckpoint]:
        """状态接口展示用：未终态，或终态尚未上报成功。"""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM task_checkpoints "
                "WHERE checkpoint_status IN ('claimed', 'running') OR report_pending = 1 "
                "ORDER BY updated_at ASC"
            ).fetchall()
        return [TaskCheckpoint(**dict(row)) for row in rows]

    def remove_checkpoint(self, task_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM task_checkpoints WHERE task_id = ?", (task_id,))

    def start_task(
        self,
        *,
        task_id: str,
        task_type: str,
        agent_id: str,
        attempt_id: str,
        payload_json: str,
        execution_timeout_seconds: int,
        external_side_effect: bool,
    ) -> None:
        """写入一次 claim 的上下文；同一 task 的新 attempt 覆盖上一条记录。"""
        now = utc_now_iso()
        with self._connect() as db:
            db.execute(
                """INSERT INTO task_checkpoints
                   (task_id, task_type, agent_id, checkpoint_status, progress,
                    created_at, updated_at, attempt_id, task_payload_json,
                    report_pending, last_report_at, execution_timeout_seconds,
                    external_side_effect)
                   VALUES (?, ?, ?, 'claimed', 0, ?, ?, ?, ?, 0, ?, ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET
                     task_type=excluded.task_type,
                     agent_id=excluded.agent_id,
                     checkpoint_status='claimed',
                     progress=0,
                     message=NULL,
                     result_json=NULL,
                     error_code=NULL,
                     updated_at=excluded.updated_at,
                     attempt_id=excluded.attempt_id,
                     task_payload_json=excluded.task_payload_json,
                     last_stage=NULL,
                     checkpoint_json=NULL,
                     final_status=NULL,
                     final_result_json=NULL,
                     error_message=NULL,
                     report_pending=0,
                     last_report_at=excluded.last_report_at,
                     started_at=NULL,
                     finished_at=NULL,
                     execution_timeout_seconds=excluded.execution_timeout_seconds,
                     external_side_effect=excluded.external_side_effect""",
                (
                    task_id, task_type, agent_id, now, now, attempt_id,
                    payload_json, now, int(execution_timeout_seconds),
                    int(bool(external_side_effect)),
                ),
            )

    def record_progress(
        self, task_id: str, attempt_id: str, progress: int, message: str
    ) -> None:
        now = utc_now_iso()
        with self._connect() as db:
            db.execute(
                """UPDATE task_checkpoints
                   SET checkpoint_status='running', progress=?, message=?,
                       last_stage=?, last_report_at=?, updated_at=?,
                       started_at=COALESCE(started_at, ?)
                   WHERE task_id=? AND attempt_id=?""",
                (int(progress), message, message, now, now, now, task_id, attempt_id),
            )

    def finish_task(
        self,
        *,
        task_id: str,
        agent_id: str,
        attempt_id: str,
        status: str,
        progress: int,
        message: str,
        result_json: Optional[str],
        error_code: Optional[str],
        error_message: Optional[str],
        retryable: bool,
        recovery_action: Optional[str],
    ) -> int:
        """同一事务写入终态与 outbox 行，返回 outbox 行 id。

        调用方必须先提交这里，再尝试上报；上报失败时 outbox 行就是待补报的结果。
        """
        now = utc_now_iso()
        with self._connect() as db:
            db.execute(
                """UPDATE task_checkpoints
                   SET checkpoint_status=?, final_status=?, final_result_json=?,
                       result_json=?, progress=?, message=?, error_code=?,
                       error_message=?, report_pending=1, finished_at=?,
                       last_report_at=?, updated_at=?
                   WHERE task_id=? AND attempt_id=?""",
                (
                    status, status, result_json, result_json, int(progress),
                    message, error_code, error_message, now, now, now,
                    task_id, attempt_id,
                ),
            )
            cursor = db.execute(
                """INSERT INTO offline_results
                   (task_id, agent_id, attempt_id, status, progress, message,
                    error_code, error_message, result_json, retryable,
                    recovery_action, queued_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id, agent_id, attempt_id, status, int(progress), message,
                    error_code, error_message, result_json, int(bool(retryable)),
                    recovery_action, now,
                ),
            )
            return cursor.lastrowid or 0

    # ---- Result Outbox ----

    def get_undelivered_results(self) -> list[OfflineResult]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM offline_results WHERE delivered = 0 ORDER BY id ASC"
            ).fetchall()
        return [OfflineResult(**dict(row)) for row in rows]

    def mark_delivered(self, result_id: int) -> None:
        """标记已投递；该任务没有其它待投递结果时清理 checkpoint。"""
        now = utc_now_iso()
        with self._connect() as db:
            row = db.execute(
                "SELECT task_id FROM offline_results WHERE id = ?", (result_id,)
            ).fetchone()
            db.execute(
                "UPDATE offline_results SET delivered = 1, delivered_at = ? WHERE id = ?",
                (now, result_id),
            )
            if row is not None:
                self._drop_settled_checkpoint(db, row["task_id"])

    def drop_result(self, result_id: int) -> None:
        """旧 attempt 已被取代：删除本地结果，不重试。"""
        with self._connect() as db:
            row = db.execute(
                "SELECT task_id FROM offline_results WHERE id = ?", (result_id,)
            ).fetchone()
            db.execute("DELETE FROM offline_results WHERE id = ?", (result_id,))
            if row is not None:
                self._drop_settled_checkpoint(db, row["task_id"])

    def record_delivery_failure(self, result_id: int) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE offline_results SET delivery_attempts = delivery_attempts + 1 "
                "WHERE id = ?",
                (result_id,),
            )

    @staticmethod
    def _drop_settled_checkpoint(db: sqlite3.Connection, task_id: str) -> None:
        remaining = db.execute(
            "SELECT COUNT(*) FROM offline_results WHERE task_id = ? AND delivered = 0",
            (task_id,),
        ).fetchone()[0]
        if remaining:
            return
        # final_status 过滤掉下载的 resume 行：它们没有可上报的终态。
        db.execute(
            "DELETE FROM task_checkpoints WHERE task_id = ? AND final_status IS NOT NULL",
            (task_id,),
        )
