import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from wt_media_agent.storage import migration


class StorageMigrationTest(unittest.TestCase):
    def test_applies_and_repeats_sqlite_migrations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "local-agent.sqlite3"

            first = migration.apply_migrations(db_path)
            second = migration.apply_migrations(db_path)

            # The order is `MIGRATIONS` order and the list is written out rather
            # than derived, so a migration appended without thought has to be
            # typed in here.
            self.assertEqual(
                [item.version for item in first],
                [
                    "0001_base",
                    "0002_task_checkpoints",
                    "0003_transfer_resume",
                    "0004_task_recovery_outbox",
                ],
            )
            self.assertEqual(second, [])

            with closing(sqlite3.connect(db_path)) as db:
                tables = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertIn("schema_migrations", tables)
                self.assertIn("agent_metadata", tables)
                self.assertIn("task_checkpoints", tables)
                self.assertIn("offline_results", tables)
                versions = [
                    row[0]
                    for row in db.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
                self.assertEqual(
                    versions,
                    [
                        "0001_base",
                        "0002_task_checkpoints",
                        "0003_transfer_resume",
                        "0004_task_recovery_outbox",
                    ],
                )

    def test_the_outbox_migration_adds_the_fields_a_terminal_result_needs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "local-agent.sqlite3"
            migration.apply_migrations(db_path)

            with closing(sqlite3.connect(db_path)) as db:
                checkpoints = _columns(db, "task_checkpoints")
                outbox = _columns(db, "offline_results")

            self.assertEqual(
                {
                    "attempt_id",
                    "task_payload_json",
                    "last_stage",
                    "checkpoint_json",
                    "final_status",
                    "final_result_json",
                    "error_message",
                    "report_pending",
                    "last_report_at",
                    "started_at",
                    "finished_at",
                    "execution_timeout_seconds",
                    "external_side_effect",
                }
                - set(checkpoints),
                set(),
            )
            self.assertEqual(
                {
                    "attempt_id",
                    "result_json",
                    "error_message",
                    "retryable",
                    "recovery_action",
                    "delivery_attempts",
                    "delivered_at",
                }
                - set(outbox),
                set(),
            )
            self.assertEqual(checkpoints["report_pending"][1], "0")
            self.assertEqual(checkpoints["execution_timeout_seconds"][1], "300")
            self.assertEqual(checkpoints["external_side_effect"][1], "0")


def _columns(db: sqlite3.Connection, table: str) -> dict[str, tuple[str, object]]:
    """Each column's declared type and its default, keyed by name."""
    return {
        row[1]: (row[2], row[4])
        for row in db.execute(f"PRAGMA table_info({table})")
    }


if __name__ == "__main__":
    unittest.main()
