import tempfile
import unittest
from pathlib import Path
from wt_media_agent.runtime.profile_operation import ProfileOperations, ProfileOperationBusy
from wt_media_agent.storage.database import Database
from wt_media_agent.storage.migration import apply_migrations
from wt_media_agent.storage.publication import PublicationJournal
from test_publication_execution import identity, snapshot

class ProfileOperationTests(unittest.TestCase):
    def test_local_operation_excludes_registration_and_other_operations(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"agent.db"
            apply_migrations(path)
            journal=PublicationJournal(Database(path))
            ident=identity()
            with journal.profile_operations.acquire(ident.bit_profile_id):
                with self.assertRaises(ProfileOperationBusy):
                    journal.register(ident,snapshot())
                with self.assertRaises(ProfileOperationBusy):
                    with journal.profile_operations.acquire(ident.bit_profile_id): pass
            self.assertTrue(journal.register(ident,snapshot()))
            # Reopening the journal must preserve the unresolved scene barrier.
            restored=PublicationJournal(Database(path))
            with self.assertRaises(ProfileOperationBusy):
                with restored.profile_operations.acquire(ident.bit_profile_id): pass
            with restored.profile_operations.acquire("unrelated-profile"): pass

    def test_local_api_returns_busy_before_browser_side_effect(self):
        from wt_media_agent.local_api.server import LocalApiServer
        from wt_media_agent.local_api.state import LocalAgentState
        from unittest.mock import Mock
        browser=Mock()
        ops=ProfileOperations(occupied=lambda profile: profile=="busy")
        api=LocalApiServer(LocalAgentState(),bitbrowser=browser,profile_operations=ops)
        for method,body in [(api.profile_close_response,{"id":"busy"}),
                            (api.profile_update_response,{"id":"busy"}),
                            (api.profile_delete_response,{"id":"busy"}),
                            (api.account_cookie_response,{"profile_id":"busy","stage":"apply"}),
                            (api.proxy_mutation_response,{"profile_id":"busy","operation":"unbind"})]:
            status,response=method(body)
            self.assertEqual(status,409)
            self.assertEqual(response["error"]["code"],"profile_operation_busy")
        self.assertEqual(browser.mock_calls,[])
