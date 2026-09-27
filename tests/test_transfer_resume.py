"""What a download leaves behind to pick itself up, and where it is kept.

Two facts live here and they are checked against each other: the record's
*shape* (`TransferResume`) and the *column* it travels in
(`task_checkpoints.transfer_state_json`). The column arrived in an appended
migration, so the interesting arm is not "can I store a string" but "a database
an older build wrote grows the column and keeps its rows".

The record is deliberately two fields. The byte offset the resumed attempt
requests is **not** one of them: it comes from the part file's own size
(`DownloadSink.resume_offset`), which cannot be ahead of the bytes that are
actually on disk. A stored offset could be -- a checkpoint written before an
unsynced tail was lost would skip exactly those bytes, and the result would be a
file of the right length and the wrong content, caught only by the digest.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from wt_media_agent.storage import migration
from wt_media_agent.storage.checkpoint_store import CheckpointStore, TaskCheckpoint
from wt_media_agent.storage.transfer_resume import TransferResume

RESUME_COLUMN = "transfer_state_json"


class ResumeRecordTest(unittest.TestCase):
    def test_the_record_carries_a_name_and_a_count(self) -> None:
        self.assertEqual(
            TransferResume("春日-42.mp4", 512).encode(),
            '{"file_name": "春日-42.mp4", "bytes_done": 512}',
        )

    def test_the_record_round_trips(self) -> None:
        record = TransferResume("春日-42.mp4", 0)

        self.assertEqual(TransferResume.decode(record.encode()), record)

    def test_no_resume_state_reads_as_no_resume(self) -> None:
        """Absent is a state: a first attempt has nothing to resume from."""
        for raw in (None, ""):
            with self.subTest(raw=raw):
                self.assertIsNone(TransferResume.decode(raw))

    def test_an_unusable_record_reads_as_no_resume_rather_than_failing(self) -> None:
        """A record this build cannot read must cost a re-download, not a failure.

        The bytes are re-fetched and checked against size and digest anyway, so
        starting over is always correct -- and it is the only one of the two
        available answers that cannot produce a corrupt file. Refusing instead
        would turn a hand-edited or older-format row into a download that never
        starts.
        """
        for raw in (
            "not json at all",
            "[]",
            '"a string"',
            '{"file_name": "a.mp4"}',
            '{"file_name": "", "bytes_done": 0}',
            '{"file_name": "a.mp4", "bytes_done": -1}',
            '{"file_name": "a.mp4", "bytes_done": 1.5}',
            '{"file_name": "a.mp4", "bytes_done": true}',
            '{"file_name": 42, "bytes_done": 0}',
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(TransferResume.decode(raw))

    def test_a_name_is_a_name_and_not_a_path(self) -> None:
        """The whole reason no absolute path is stored.

        The part file's location is derived at resume time from the save
        directory in force *then*, so a resume record that carried a path would
        pin the download to a directory the operator may have since changed --
        and would be the one place a local absolute path entered a database
        CHG-061 §8 keeps paths out of.
        """
        for name in ("/tmp/elsewhere.mp4", "sub/dir.mp4", "sub\\dir.mp4", "..", "."):
            with self.subTest(name=name):
                self.assertIsNone(TransferResume.decode(f'{{"file_name": "{name}", "bytes_done": 0}}'))

    def test_a_name_the_sink_would_produce_is_accepted(self) -> None:
        """The positive control for the arm above: the guard can say yes.

        Without it, a `decode` that returned `None` unconditionally would make
        every rejection case pass.
        """
        self.assertIsNotNone(TransferResume.decode('{"file_name": "春日-42.mp4", "bytes_done": 0}'))


class CheckpointColumnTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "agent.db"

    def _checkpoint(self, **overrides: object) -> TaskCheckpoint:
        fields: dict[str, object] = {
            "task_id": "task-1",
            "task_type": "material_download_task",
            "agent_id": "agent-1",
            "checkpoint_status": "running",
        }
        fields.update(overrides)
        return TaskCheckpoint(**fields)  # type: ignore[arg-type]

    def test_the_resume_state_round_trips_through_the_checkpoint(self) -> None:
        migration.apply_migrations(self.db_path)
        store = CheckpointStore(self.db_path)
        record = TransferResume("春日-42.mp4", 4096)

        store.save_checkpoint(self._checkpoint(transfer_state_json=record.encode()))

        stored = store.get_checkpoint("task-1")
        assert stored is not None
        self.assertEqual(TransferResume.decode(stored.transfer_state_json), record)

    def test_a_checkpoint_that_never_resumed_reads_back_as_none(self) -> None:
        """Every checkpoint written before this column existed looks like this."""
        migration.apply_migrations(self.db_path)
        store = CheckpointStore(self.db_path)

        store.save_checkpoint(self._checkpoint())

        stored = store.get_checkpoint("task-1")
        assert stored is not None
        self.assertIsNone(stored.transfer_state_json)

    def test_the_resume_state_survives_an_update_of_the_same_checkpoint(self) -> None:
        """`save_checkpoint` replaces the row; the resume state must come along.

        It is written on a later pass than the claim (the first checkpoint is
        saved at progress 0, before the executor has a name to record), so the
        path that matters is the second write.
        """
        migration.apply_migrations(self.db_path)
        store = CheckpointStore(self.db_path)
        store.save_checkpoint(self._checkpoint(progress=0))

        record = TransferResume("春日-42.mp4", 1234)
        store.save_checkpoint(
            self._checkpoint(progress=25, transfer_state_json=record.encode())
        )

        stored = store.get_checkpoint("task-1")
        assert stored is not None
        self.assertEqual(TransferResume.decode(stored.transfer_state_json), record)

    def test_a_database_written_by_the_previous_build_gains_the_column(self) -> None:
        """The upgrade arm, against the previous build's actual schema.

        The first two migrations are applied and a row is planted into them, the
        way the build before this one left the file; the full set is then
        applied. Two claims: only the new migration runs, and the row the older
        build wrote is still there and readable through the current store.
        """
        previous = tuple(
            item for item in migration.MIGRATIONS if item.version != "0003_transfer_resume"
        )
        real = migration.MIGRATIONS
        migration.MIGRATIONS = previous
        try:
            applied_before = migration.apply_migrations(self.db_path)
        finally:
            migration.MIGRATIONS = real
        self.assertEqual(
            [item.version for item in applied_before], ["0001_base", "0002_task_checkpoints"]
        )
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                """
                INSERT INTO task_checkpoints (
                    task_id, task_type, agent_id, checkpoint_status, progress,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("old-1", "publish", "agent-1", "running", 40,
                 "2026-09-01T00:01:00Z", "2026-09-01T00:01:00Z"),
            )

        applied = migration.apply_migrations(self.db_path)

        self.assertEqual([item.version for item in applied], ["0003_transfer_resume"])
        stored = CheckpointStore(self.db_path).get_checkpoint("old-1")
        assert stored is not None
        self.assertEqual(stored.progress, 40)
        self.assertIsNone(stored.transfer_state_json)

    def test_the_column_is_declared_like_the_optional_ones_beside_it(self) -> None:
        """Nullable, defaulting to nothing -- not `NOT NULL` with a placeholder.

        A `NOT NULL` default of `''` would make "never resumed" and "resumed
        with nothing" the same row, and an empty string is not a record. Checked
        against `message`'s row rather than against `"TEXT"/0/'NULL'` spelled
        out: what this arm is about is that the new column is shaped like the
        optional columns already in the table, and SQLite reports `DEFAULT NULL`
        as the text `'NULL'` rather than as a null.
        """
        migration.apply_migrations(self.db_path)

        with closing(sqlite3.connect(self.db_path)) as db:
            # (cid, name, type, notnull, dflt_value, pk)
            columns = {
                row[1]: tuple(row)
                for row in db.execute("PRAGMA table_info(task_checkpoints)")
            }

        self.assertIn(RESUME_COLUMN, columns)
        existing = columns["message"]
        added = columns[RESUME_COLUMN]
        self.assertEqual(
            (added[2], added[3], added[4], added[5]),
            (existing[2], existing[3], existing[4], existing[5]),
        )


if __name__ == "__main__":
    unittest.main()
