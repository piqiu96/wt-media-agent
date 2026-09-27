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
            # typed in here. Extended for `0003_transfer_resume` (CHG-061 T-04)
            # in the same commit that appended it.
            self.assertEqual(
                [item.version for item in first],
                ["0001_base", "0002_task_checkpoints", "0003_transfer_resume"],
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
                    ["0001_base", "0002_task_checkpoints", "0003_transfer_resume"],
                )


if __name__ == "__main__":
    unittest.main()
