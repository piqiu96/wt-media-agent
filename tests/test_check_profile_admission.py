"""Cross-Cloud local Profile admission uses the same real SQLite boundary."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from wt_media_agent.storage.publication import ExecutionIdentity, JournalConflict
from test_publication_execution import snapshot
import test_publication_check_job as check_fixtures
from wt_media_agent.runner.publication_check import PublicationCheckJob

class CheckProfileAdmissionTests(unittest.TestCase):
    setUp=check_fixtures.PublicCheckJobTests.setUp
    tearDown=check_fixtures.PublicCheckJobTests.tearDown
    def hold_check(self):
        state=self.job.records.begin('dev','9',{})
        self.job.records.transition('dev','9',state['request_key'],'claiming','active',claim=self.claim)
        self.job.records.transition('dev','9',state['request_key'],'active','possible_open',endpoint='127.0.0.1:9999')
    def test_prior_check_blocks_publish_in_another_cloud_after_restart(self):
        from wt_media_agent.storage.publication import PublicationJournal
        self.hold_check()
        other=PublicationJournal(self.db)
        i=ExecutionIdentity('prod','9','2','2','original-profile')
        with self.assertRaises(JournalConflict):other.register(i,snapshot())
    def test_prior_check_blocks_check_in_another_cloud_without_touching_scene(self):
        self.hold_check()
        other=PublicationCheckJob(self.cloud,self.bit,self.journal,lambda _:lambda:self.checker,cloud_env='prod')
        other.tick('new-token','9',current=lambda:True)
        self.bit.open_profile_for_check.assert_not_called()
        self.bit.close_profile_verified.assert_not_called()
        self.cloud.observe_publication.assert_not_called()
        self.cloud.settle_unreceived_publication_check.assert_called_once()
    def test_atomic_check_and_publish_registration_only_one_wins(self):
        from wt_media_agent.storage.publication import PublicationJournal
        state=self.job.records.begin('dev','9',{})
        other=PublicationJournal(self.db)
        gate=Barrier(2)
        def publish():
            gate.wait()
            try:return other.register(ExecutionIdentity('prod','9','2','2','original-profile'),snapshot())
            except JournalConflict:return False
        def check():
            gate.wait()
            return self.job.records.transition('dev','9',state['request_key'],'claiming','active',claim=self.claim)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(publish);b=pool.submit(check)
            self.assertEqual(sorted([a.result(),b.result()]),[False,True])
    def test_noncurrent_possible_open_is_not_a_permission_to_close(self):
        self.hold_check()
        self.job.tick('old-token','9',current=lambda:False)
        self.bit.close_profile_verified.assert_not_called()
        self.cloud.release_publication_check.assert_not_called()
