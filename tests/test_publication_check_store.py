import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from wt_media_agent.storage.database import Database
from wt_media_agent.storage.migration import apply_migrations
from wt_media_agent.storage.publication_check import PublicationCheckStore

class CheckStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'checks.db'
        self.db = Database(self.path)
        apply_migrations(self.db)
        self.store = PublicationCheckStore(self.db)
    def tearDown(self):
        self.db.close()
        self.temp.cleanup()
    def test_restart_scope_and_late_active_cannot_reverse_settlement(self):
        state = self.store.begin('dev', '9', {'automatic_limit': 1, 'window_limit': 1})
        key = state['request_key']
        self.assertTrue(self.store.transition('dev', '9', key, 'claiming', 'settling'))
        self.db.close()
        self.db = Database(self.path)
        self.store = PublicationCheckStore(self.db)
        self.assertEqual(self.store.get('dev', '9')['phase'], 'settling')
        self.assertIsNone(self.store.get('dev', '10'))
        self.assertIsNone(self.store.get('prod', '9'))
        self.assertFalse(self.store.transition('dev', '9', key, 'claiming', 'active', claim={}))
        self.assertTrue(self.store.transition('dev', '9', key, 'settling', 'settled'))
        new = self.store.begin('dev', '9', {})
        self.assertNotEqual(new['request_key'], key)
        self.assertFalse(self.store.transition('dev', '9', key, 'claiming', 'active', claim={}))
    def test_accept_and_settle_are_exclusive_database_cas(self):
        key = self.store.begin('dev', '9', {})['request_key']
        def compete(phase):
            return self.store.transition('dev', '9', key, 'claiming', phase)
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(compete, ['active', 'settling'])), [False, True])
    def test_possible_open_and_packet_survive_restart(self):
        key = self.store.begin('dev', '9', {})['request_key']
        self.store.transition('dev', '9', key, 'claiming', 'active', claim={'generation':'3'})
        self.store.transition('dev', '9', key, 'active', 'possible_open')
        self.store.transition('dev', '9', key, 'possible_open', 'packet', packet={'release_only':True})
        self.db.close()
        self.db = Database(self.path)
        self.store = PublicationCheckStore(self.db)
        self.assertEqual(self.store.get('dev','9')['packet'], {'release_only':True})

    def test_held_profile_is_visible_until_settlement(self):
        key = self.store.begin('dev','9',{})['request_key']
        self.store.transition('dev','9',key,'claiming','active',claim={'attempt':{'input_snapshot':{'bit_profile_id':'original'}}})
        self.assertTrue(self.store.profile_occupied('original'))
        self.store.transition('dev','9',key,'active','settled')
        self.assertFalse(self.store.profile_occupied('original'))

    def test_retained_windows_count_all_owners_but_not_closed_packets(self):
        for env,user in [('dev','9'),('prod','10')]:
            key=self.store.begin(env,user,{})['request_key']
            self.store.transition(env,user,key,'claiming','active',claim={'attempt':{'input_snapshot':{'bit_profile_id':user}}})
        self.assertEqual(self.store.retained_window_count(),2)
        state=self.store.get('dev','9')
        self.store.transition('dev','9',state['request_key'],'active','packet',packet={'release_only':True,'profile_disposition':'closed'})
        self.assertEqual(self.store.retained_window_count(),1)
        self.store.begin('dev','11',{})
        self.assertEqual(self.store.retained_window_count(),1)
