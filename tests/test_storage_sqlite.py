import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from wt_media_agent.storage import CheckpointStore, OfflineResult, TaskCheckpoint
from wt_media_agent.storage import checkpoint_store
from wt_media_agent.storage.migration import apply_migrations
from wt_media_agent.storage.sqlite import connect


class SqliteConnectTest(unittest.TestCase):
    """Locks the two properties `storage/sqlite.py` exists to own."""

    def test_rows_are_addressable_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "t.sqlite3")
            try:
                conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
                conn.execute("INSERT INTO t VALUES (1, 'x')")
                row = conn.execute("SELECT * FROM t").fetchone()
                self.assertEqual(row["a"], 1)
                self.assertEqual(row["b"], "x")
            finally:
                conn.close()

    def test_wal_is_enabled_and_persists_across_connections(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.sqlite3"
            connect(db).close()
            # A plain connection must observe WAL, otherwise the pragma was
            # not actually persisted to the database header.
            with closing(sqlite3.connect(db)) as plain:
                mode = plain.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")

    def test_connect_accepts_str_and_path_identically(self):
        with tempfile.TemporaryDirectory() as tmp:
            for value in (Path(tmp) / "s.sqlite3", str(Path(tmp) / "s.sqlite3")):
                connect(value).close()


class StoragePackageSurfaceTest(unittest.TestCase):
    def test_reexports_are_the_same_objects(self):
        self.assertIs(CheckpointStore, checkpoint_store.CheckpointStore)
        self.assertIs(TaskCheckpoint, checkpoint_store.TaskCheckpoint)
        self.assertIs(OfflineResult, checkpoint_store.OfflineResult)

    def test_checkpoint_store_still_round_trips(self):
        """Guards the extraction: the store still works through `connect`."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "agent.db"
            apply_migrations(db)
            store = CheckpointStore(db)
            store.save_checkpoint(
                TaskCheckpoint(
                    task_id="t1",
                    task_type="noop",
                    agent_id="a1",
                    checkpoint_status="running",
                    progress=7,
                    message="m",
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:00:00Z",
                )
            )
            got = store.get_checkpoint("t1")
            self.assertIsNotNone(got)
            self.assertEqual(got.task_id, "t1")
            self.assertEqual(got.progress, 7)
            self.assertEqual(got.checkpoint_status, "running")
            self.assertEqual(
                [cp.task_id for cp in store.get_incomplete_checkpoints()], ["t1"]
            )


if __name__ == "__main__":
    unittest.main()
