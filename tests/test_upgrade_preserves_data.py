"""T-07 of CHG-20260923-059: an upgrade does not overwrite the operator's data.

The Agent's half of the property 「升级不覆盖用户设置/SQLite/检查点/待回传结果」. The
upgrade action this repository actually has is **the storage migration** — a new
build starting on a database an older build wrote — so that is what these arms
run, against the declared paths rather than a scratch directory of their own.

Two things are pinned, and they are different claims:

1. **Additivity** — a migration applied over an existing database leaves the rows
   that are already in it alone. Today's `MIGRATIONS` are all additive
   (`CREATE TABLE IF NOT EXISTS`), so this arm cannot fail on the current
   statements; it fails the moment a future migration drops or rewrites a table,
   which is the failure the milestone forbids. `a_new_migration_leaves_existing_rows_alone`
   is therefore written against the mechanism (a migration added at run time),
   not against a hypothetical statement.
2. **The write set is a path** — an upgrade writes the declared database file and
   creates no path beside it. Two comparisons, because they see different things:
   what the upgrade itself changed (before/after the second apply), and what the
   directory holds measured against **the state before this test touched it**.
   The second one is not redundant — the first cannot see a path that *every*
   apply creates, because this arm's own planting step applies once and would
   have created it first. The same comparison catches a file planted on purpose,
   which is the positive control inside it.

Red for both arms comes from mutation, not from a missing implementation: the
migration is already additive and already confined, so a first cut that fails
does not exist. The recorded mutations (an upgrade that removes a table, one that
prunes "stale" rows, and one that stages a copy beside the database) are what
show these arms can fail.
"""

from __future__ import annotations

import contextlib
import io
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path

from wt_media_agent.storage import migration

from support import isolated_paths


def _rows(db_path: Path, table: str) -> list[tuple]:
    with closing(sqlite3.connect(db_path)) as db:
        return [
            tuple(row)
            for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")  # noqa: S608
        ]


def _paths_under(root: Path) -> set[str]:
    return {
        str(path.relative_to(root))
        for path in sorted(root.rglob("*"))
    }


class UpgradePreservesDataTest(unittest.TestCase):
    """The migration as an upgrade: additive, and confined to one path."""

    def _planted(self, db_path: Path) -> None:
        """A database an older build left behind, with the operator's rows in it."""
        migration.apply_migrations(db_path)
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute(
                "INSERT INTO agent_metadata (key, value, updated_at) VALUES (?, ?, ?)",
                ("installed_at", "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z"),
            )
            db.execute(
                """
                INSERT INTO task_checkpoints (
                    task_id, task_type, agent_id, checkpoint_status, progress,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("task-1", "publish", "agent-1", "running", 40,
                 "2026-09-01T00:01:00Z", "2026-09-01T00:01:00Z"),
            )
            db.execute(
                """
                INSERT INTO offline_results (
                    task_id, agent_id, status, progress, queued_at, delivered
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("task-2", "agent-1", "pending", 0, "2026-09-01T00:02:00Z", 0),
            )

    def test_the_database_is_the_declared_path_under_the_data_directory(self) -> None:
        """`default_db_path` is the one rule, and it is the data directory's own.

        The arm exists so a later change that moves the database — a second rule
        computed somewhere else — is a failure here rather than a database nobody
        migrates.
        """
        with isolated_paths() as paths:
            self.assertEqual(migration.default_data_dir(), paths.data)
            self.assertEqual(
                migration.default_db_path(),
                paths.data / migration.DEFAULT_DB_NAME,
            )

    def test_a_new_migration_leaves_existing_rows_alone(self) -> None:
        """The additivity arm, written against the mechanism rather than a guess.

        A migration added at run time stands in for the one a future release will
        ship. It is additive on purpose; the arm's subject is that the rows the
        older build wrote are still there afterwards, byte for byte.
        """
        with isolated_paths() as paths:
            db_path = migration.default_db_path()
            self._planted(db_path)
            before = {
                table: _rows(db_path, table)
                for table in ("agent_metadata", "task_checkpoints", "offline_results")
            }
            self.assertTrue(all(before.values()), "the planting must be visible")

            extra = migration.Migration(
                version="0003_additive_probe",
                name="additive_probe",
                statements=(
                    """
                    CREATE TABLE IF NOT EXISTS upgrade_probe (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        note TEXT NOT NULL
                    )
                    """,
                ),
            )
            real = migration.MIGRATIONS
            migration.MIGRATIONS = (*real, extra)
            try:
                applied = migration.apply_migrations(db_path)
            finally:
                migration.MIGRATIONS = real

            self.assertEqual(
                [item.version for item in applied],
                ["0003_additive_probe"],
                "only the new migration is applied on an upgrade",
            )
            for table, rows in before.items():
                self.assertEqual(_rows(db_path, table), rows, f"{table} lost rows")

    def test_the_upgrade_writes_the_database_and_no_other_path(self) -> None:
        """The path criterion: the upgrade's write set is one declared file.

        Everything else under the data directory — a file the operator left, a
        directory belonging to something else — is byte-identical afterwards, and
        no new path appears. `DEFAULT_DB_NAME` is the only name the upgrade may
        bring into being, and it is the name the configuration layer declared.
        """
        with isolated_paths() as paths:
            db_path = migration.default_db_path()
            # The directory as this test found it, before the planting below
            # applies anything. Every path the migration brings into being is
            # `after - empty - planted`, so an artifact created on *both* applies
            # is seen too — the before/after pair further down cannot see it,
            # because the planting step would have created it first.
            empty = _paths_under(paths.data)
            planted_paths = {
                "operator-notes.txt",
                "versions",
                "versions/0.2.2",
                "versions/0.2.2/payload.bin",
            }
            self._planted(db_path)

            # Falsifiable by construction: a file under the data directory that
            # the migration has no business touching.
            kept = paths.data / "operator-notes.txt"
            kept.write_text("不要覆盖我\n", encoding="utf-8")
            nested = paths.data / "versions" / "0.2.2"
            nested.mkdir(parents=True)
            payload = nested / "payload.bin"
            payload.write_bytes(b"\x00\x01payload")

            before_bytes = {path: path.read_bytes() for path in (kept, payload)}
            before_paths = _paths_under(paths.data)

            applied = migration.apply_migrations(db_path)

            self.assertEqual(applied, [], "nothing new to apply")
            after_paths = _paths_under(paths.data)

            self.assertEqual(
                after_paths,
                before_paths,
                "the upgrade wrote a path it did not declare",
            )
            self.assertEqual(
                after_paths - empty - planted_paths,
                {migration.DEFAULT_DB_NAME},
                "the migration created a path of its own beside the declared one",
            )
            for path, content in before_bytes.items():
                self.assertEqual(path.read_bytes(), content, f"{path.name} changed")
            self.assertEqual(kept.read_text(encoding="utf-8"), "不要覆盖我\n")

            # Positive control: the comparison above would be worthless if it
            # could not see a new file at all.
            (paths.data / "planted-after.txt").write_text("x", encoding="utf-8")
            self.assertNotEqual(
                _paths_under(paths.data),
                after_paths,
                "the snapshot must be able to see a new path",
            )
            self.assertIn(
                migration.DEFAULT_DB_NAME,
                after_paths,
                "the declared database is the one path the upgrade may create",
            )

    def test_the_console_script_an_installer_runs_is_the_same_operation(self) -> None:
        """`migration.main` — the entry point `migrate-storage.sh` calls — over
        an existing database: exit 0, the rows still there, no path beside the
        database, and a report that names the file it touched. A second entry
        point that disagreed with `apply_migrations` would be an upgrade path
        nothing else exercises.
        """
        with isolated_paths() as paths:
            db_path = migration.default_db_path()
            self._planted(db_path)
            before = _rows(db_path, "task_checkpoints")
            before_paths = _paths_under(paths.data)

            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                code = migration.main(["--db-path", str(db_path)])

            self.assertEqual(code, 0)
            self.assertEqual(_rows(db_path, "task_checkpoints"), before)
            self.assertEqual(_paths_under(paths.data), before_paths)
            self.assertIn(str(db_path), printed.getvalue())


if __name__ == "__main__":
    unittest.main()
