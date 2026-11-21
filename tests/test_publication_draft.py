"""Real SQLite persistence and compare-and-set races for new publication input."""
import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import select, func
from wt_media_agent.storage.database import Database
from wt_media_agent.storage.migration import apply_migrations
from wt_media_agent.storage.publication_draft import PublicationDraftStore, DraftConflict
from wt_media_agent.storage.models import PublicationDraftModel


class PublicationDraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'agent.sqlite3'
        self.db = Database(self.path)
        apply_migrations(self.db)
        self.store = PublicationDraftStore(self.db, environment='dev')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_restart_and_scope_isolation(self):
        value = self.store.put('9', 'task', {'description': '人工稿'}, '0')
        self.assertEqual(value['revision'], '1')
        self.db.close()
        self.db = Database(self.path)
        apply_migrations(self.db)
        self.store = PublicationDraftStore(self.db, environment='dev')
        self.assertEqual(self.store.get('9', 'task')['payload'], {'description': '人工稿'})
        self.assertIsNone(self.store.get('10', 'task')['payload'])
        self.assertIsNone(PublicationDraftStore(self.db, environment='prod').get('9', 'task')['payload'])

    def test_clear_preserves_revision_and_rejects_old_autosave(self):
        self.store.put('9', 'task', {'description': '旧稿'}, '0')
        self.store.clear('9', 'task', '1')
        with self.assertRaises(DraftConflict):
            self.store.put('9', 'task', {'description': '迟到旧稿'}, '1')
        self.assertEqual(self.store.get('9', 'task')['revision'], '2')
        self.assertIsNone(self.store.get('9', 'task')['payload'])
        self.store.put('9', 'task', {'description': '新稿'}, '2')
        with self.db.session() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(PublicationDraftModel)), 1)

    def test_lost_reply_replay_returns_current_result_only(self):
        first = self.store.put('9', 'task', {'description': '人工稿'}, '0')
        self.assertEqual(self.store.put('9', 'task', {'description': '人工稿'}, '0'), first)
        with self.assertRaises(DraftConflict):
            self.store.put('9', 'task', {'description': '不同稿'}, '0')
        cleared = self.store.clear('9', 'task', '1')
        self.assertEqual(self.store.clear('9', 'task', '1'), cleared)

    def test_two_tabs_cannot_overwrite_each_other(self):
        def save(text):
            try:
                self.store.put('9', 'task', {'description': text}, '0')
                return True
            except DraftConflict:
                return False
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(save, ['甲', '乙'])), [False, True])

    def test_payload_and_scope_validation(self):
        for user, kind, payload, revision in [
            ('', 'task', {}, '0'), ('9', 'plan', {}, '0'), ('9', 'task', [], '0'),
            ('9', 'task', {'description': 'x' * (2 * 1024 * 1024)}, '0'),
            ('9', 'task', {}, 0), ('9', 'task', {}, '-1'),
            ('9', 'task', {'description': float('nan')}, '0'),
        ]:
            with self.subTest(user=user, kind=kind, revision=revision):
                with self.assertRaises(ValueError):
                    self.store.put(user, kind, payload, revision)
