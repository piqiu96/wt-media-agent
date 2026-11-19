"""Storage behavior stays independent of the selected driver's mechanics."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from wt_media_agent.storage import database as database_module
from wt_media_agent.storage.database import Database
from wt_media_agent.storage.checkpoint_store import CheckpointStore
from wt_media_agent.storage.migration import apply_migrations
from wt_media_agent.storage.publication import ExecutionIdentity, PublicationJournal
from test_publication_execution import snapshot


class StorageDialectTests(unittest.TestCase):
    def test_data_directory_uses_existing_database_name(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(data_dir=Path(directory))
            self.addCleanup(db.close)
            apply_migrations(db)
            self.assertTrue((Path(directory) / 'local-agent.sqlite3').is_file())
            with db.engine.connect() as connection:
                self.assertEqual(connection.exec_driver_sql('PRAGMA foreign_keys').scalar(), 1)
                self.assertEqual(connection.exec_driver_sql('PRAGMA synchronous').scalar(), 2)

    def test_unimplemented_driver_fails_before_creating_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'must-not-create' / 'agent.db'
            with patch.object(database_module, 'DATABASE_TYPE', 'unsupported', create=True):
                with self.assertRaisesRegex(ValueError, 'unsupported database'):
                    Database(path)
            self.assertFalse(path.parent.exists())

    def test_pending_count_is_scoped_and_changes_only_after_original_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / 'agent.db')
            self.addCleanup(db.close)
            apply_migrations(db)
            journal = PublicationJournal(db)
            identity = ExecutionIdentity('dev', '3', '9', '1', 'profile')
            journal.register(identity, snapshot())
            packet = journal.save_result(identity, {'status': 'uncertain'}, report_seq=1)
            store = CheckpointStore(db, cloud_env='dev', user_id=lambda: '3')
            self.assertEqual(store.count_undelivered_results(), 1)
            self.assertEqual(store.for_user('4').count_undelivered_results(), 0)
            self.assertEqual(CheckpointStore(db, cloud_env='prod', user_id=lambda: '3').count_undelivered_results(), 0)
            self.assertFalse(journal.record_delivery('dev', '3', packet, accepted=False))
            self.assertEqual(store.count_undelivered_results(), 1)
            self.assertTrue(journal.record_delivery('dev', '3', packet, accepted=True))
            self.assertEqual(store.count_undelivered_results(), 0)
