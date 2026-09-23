"""SQLite-backed checkpoint store for task recovery and offline result queue."""

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


class CheckpointStore:
    """Persists task checkpoints and offline results to SQLite."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        return connect(self._db_path)

    # ---- Task Checkpoints ----

    def save_checkpoint(self, cp: TaskCheckpoint) -> None:
        now = utc_now_iso()
        with self._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO task_checkpoints
                   (task_id, task_type, agent_id, checkpoint_status, progress,
                    message, result_json, error_code, created_at, updated_at, retry_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, ?), ?, ?)""",
                (
                    cp.task_id, cp.task_type, cp.agent_id, cp.checkpoint_status,
                    cp.progress, cp.message, cp.result_json, cp.error_code,
                    cp.created_at or now, cp.created_at or now, now, cp.retry_count,
                ),
            )

    def get_checkpoint(self, task_id: str) -> Optional[TaskCheckpoint]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM task_checkpoints WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        return TaskCheckpoint(**dict(row))

    def get_incomplete_checkpoints(self) -> list[TaskCheckpoint]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM task_checkpoints WHERE checkpoint_status IN ('claimed', 'running')"
            ).fetchall()
        return [TaskCheckpoint(**dict(row)) for row in rows]

    def remove_checkpoint(self, task_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM task_checkpoints WHERE task_id = ?", (task_id,))

    # ---- Offline Results Queue ----

    def enqueue_result(self, result: OfflineResult) -> int:
        now = utc_now_iso()
        with self._connect() as db:
            cursor = db.execute(
                """INSERT INTO offline_results
                   (task_id, agent_id, status, progress, message, error_code, queued_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (result.task_id, result.agent_id, result.status, result.progress,
                 result.message, result.error_code, now),
            )
            return cursor.lastrowid or 0

    def get_undelivered_results(self) -> list[OfflineResult]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM offline_results WHERE delivered = 0 ORDER BY id ASC"
            ).fetchall()
        return [OfflineResult(**dict(row)) for row in rows]

    def mark_delivered(self, result_id: int) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE offline_results SET delivered = 1 WHERE id = ?", (result_id,)
            )
