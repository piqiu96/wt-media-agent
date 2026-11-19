from wt_media_agent.storage.database import Database
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from wt_media_agent.storage import CheckpointStore, TaskCheckpoint
from wt_media_agent.storage import checkpoint_store
from wt_media_agent.storage.migration import apply_migrations


class SqliteConnectTest(unittest.TestCase):
    def test_sqlalchemy_rows_and_wal_are_available_through_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            database=Database(Path(tmp)/'agent.db')
            self.addCleanup(database.close)
            with database.engine.begin() as connection:
                connection.exec_driver_sql('CREATE TABLE t(a INTEGER,b TEXT)')
                connection.exec_driver_sql("INSERT INTO t VALUES(1,'x')")
                row=connection.exec_driver_sql('SELECT * FROM t').mappings().one()
                self.assertEqual(dict(row),{'a':1,'b':'x'})
                self.assertEqual(connection.exec_driver_sql('PRAGMA journal_mode').scalar(),'wal')

class StoragePackageSurfaceTest(unittest.TestCase):
    def test_reexports_are_the_same_objects(self):
        self.assertIs(CheckpointStore, checkpoint_store.CheckpointStore)
        self.assertIs(TaskCheckpoint, checkpoint_store.TaskCheckpoint)

    def test_checkpoint_store_still_round_trips(self):
        """Guards the extraction: the store still works through ORM sessions."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "agent.db"
            apply_migrations(db)
            store = CheckpointStore(Database(db))
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
            got = store.get_checkpoint("t1", task_type="noop")
            self.assertIsNotNone(got)
            self.assertEqual(got.task_id, "t1")
            self.assertEqual(got.progress, 7)
            self.assertEqual(got.checkpoint_status, "running")
            self.assertEqual(
                [cp.task_id for cp in store.get_incomplete_checkpoints()], ["t1"]
            )


if __name__ == "__main__":
    unittest.main()
