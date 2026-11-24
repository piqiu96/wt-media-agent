import copy
import tempfile
from pathlib import Path
from threading import Event, Thread
from wt_media_agent.storage.database import Database
from wt_media_agent.storage.migration import apply_migrations
from wt_media_agent.storage.publication_check import PublicationCheckStore
import unittest
from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import Mock

from wt_media_agent.platforms.publication_check import PublicWorkObservation
from wt_media_agent.runner.publication_check import PublicationCheckJob
from wt_media_agent.platforms.baidu.publication_check import BaiduPublicationChecker
from wt_media_agent.clients.baidu.publication import BaiduPublicationApiError
from wt_media_agent.runtime.profile_operation import ProfileOperations

TARGET='https://baijiahao.baidu.com/builder/preview/s?id=1882371730190912814'
PUBLIC='https://mbd.baidu.com/newspage/data/videolanding?nid=sv_7000395122085014122'
class PublicCheckJobTests(unittest.TestCase):
    def setUp(self):
        self.source={'profile_id':'37','bit_profile_id':'original-profile','bit_main_user_id':'bit-owner','platform_uid':'6572476037','content':{'description':'确认标题','topics':'#话题','declaration':'无需声明'}}
        self.claim={'generation':'3','expected_revision':'7','task':{'id':'9007199254740993','operator_id':'9','status':'reviewing','platform':'baidu','revision':'7','current_attempt_id':'9007199254740995','platform_account_uid':'6572476037','platform_submission_id':'1882371730190912814','target_url':None},'attempt':{'id':'9007199254740995','publication_id':'9007199254740993','executor_user_id':'9','profile_id':'37','input_snapshot':self.source}}
        self.claim.update(reservation_id='41',reservation_state='active',browser_fallback_allowed=True,replayed=False)
        self.cloud=Mock();self.cloud.claim_publication_check.return_value=self.claim
        self.ack=dict(publication_id=self.claim['task']['id'],attempt_id=self.claim['attempt']['id'],generation='3',reservation_id='41',released=True)
        self.cloud.observe_publication.return_value=self.ack
        self.cloud.release_publication_check.return_value=self.ack
        self.cloud.settle_unreceived_publication_check.side_effect=lambda token,key: dict(request_key=key,reservation_state='cancelled')
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'agent.db'
        self.db=Database(self.path);apply_migrations(self.db)
        self.bit=Mock();self.bit.scan_profiles.return_value=SimpleNamespace(profiles=[SimpleNamespace(bit_profile_id='original-profile',main_user_id='bit-owner')])
        def opened(profile, *, before_open=lambda:None):
            before_open();return '127.0.0.1:9999'
        self.bit.open_profile_for_check.side_effect=opened
        self.bit.close_profile_verified.return_value=True
        self.bit.is_profile_closed.return_value=True
        self.reader=Mock();self.reader.read.return_value=PublicWorkObservation('published','指定作品已经公开','原账号、指定原链接、匿名媒体、确认内容和平台时间均已匹配','1882371730190912814',TARGET,'7000395122085014122',PUBLIC,datetime(2026,11,20,10,12,53,tzinfo=timezone.utc))
        self.journal=SimpleNamespace(profile_operations=ProfileOperations(),profile_in_use=lambda profile:False,database=self.db)
        self.checker=BaiduPublicationChecker()
        self.checker._read_api=Mock(side_effect=BaiduPublicationApiError())
        self.checker._read_browser=self.reader.read
        self.job=PublicationCheckJob(self.cloud,self.bit,self.journal,lambda platform:lambda:self.checker,cloud_env='dev')
    def tearDown(self):
        self.db.close();self.temp.cleanup()
    def state(self, user='9'):
        return self.job.records.get('dev',user)
    def restart(self):
        self.db.close();self.db=Database(self.path)
        self.journal.database=self.db
        self.job=PublicationCheckJob(self.cloud,self.bit,self.journal,lambda platform:lambda:self.checker,cloud_env='dev')
    def run_check(self):
        self.job.tick('token','9',current=lambda:True)
    def test_public_original_input_and_closed_reservation_are_reported(self):
        original=copy.deepcopy(self.claim)
        self.run_check()
        packet=self.cloud.observe_publication.call_args.args[2]
        self.assertEqual(packet['reservation_id'],'41')
        self.assertEqual(packet['profile_disposition'],'closed')
        self.assertEqual(packet['observation']['conclusion'],'published')
        self.assertEqual(packet['observation']['published_at'],'2026-11-20T10:12:53Z')
        self.assertTrue(packet['observation']['evidence']['scene_closed'])
        self.assertEqual(self.state()['phase'],'settled')
        self.assertEqual(self.claim,original)
        self.reader.read.assert_called_once()
    def test_close_failure_retains_reservation_without_observation(self):
        self.bit.close_profile_verified.return_value=False
        self.run_check()
        self.cloud.observe_publication.assert_not_called()
        self.cloud.release_publication_check.assert_not_called()
        self.assertEqual(self.state()['phase'],'possible_open')
    def test_initial_foreign_session_does_not_claim(self):
        self.job.tick('token','9',current=lambda:False)
        self.cloud.claim_publication_check.assert_not_called()
        self.assertIsNone(self.state())
    def test_foreign_claim_never_opens_and_original_key_remains_recoverable(self):
        self.claim['attempt']['executor_user_id']='10'
        with self.assertRaises(ValueError):self.run_check()
        self.bit.open_profile_for_check.assert_not_called()
        self.assertEqual(self.state()['phase'],'claiming')
    def test_unresolved_profile_never_opens_or_releases(self):
        self.journal.profile_in_use=lambda profile:True
        self.job.publication_occupied=self.journal.profile_in_use
        self.run_check()
        self.bit.open_profile_for_check.assert_not_called()
        self.cloud.observe_publication.assert_not_called()
        self.assertEqual(self.state()['phase'],'active')
    def test_changed_session_still_closes_and_releases_original(self):
        active=[True]
        def read(*args,**kwargs):
            active[0]=False
            return PublicWorkObservation('uncertain','changed','read interrupted')
        self.reader.read.side_effect=read
        self.job.tick('old-token','9',current=lambda:active[0])
        self.bit.close_profile_verified.assert_called_once()
        self.cloud.observe_publication.assert_not_called()
        self.assertTrue(self.cloud.release_publication_check.call_args.args[2]['release_only'])
        self.assertEqual(self.state()['phase'],'settled')
    def test_lost_observation_reply_restarts_exact_packet_not_browser(self):
        self.cloud.observe_publication.side_effect=TimeoutError('reply lost')
        self.run_check()
        packet=copy.deepcopy(self.state()['packet'])
        self.restart()
        self.cloud.observe_publication.side_effect=None
        self.run_check()
        self.assertEqual(self.cloud.observe_publication.call_args.args[2],packet)
        self.bit.open_profile_for_check.assert_called_once()
        self.assertEqual(self.state()['phase'],'settled')
    def test_crash_after_open_only_recovers_original_close(self):
        self.bit.close_profile_verified.return_value=False
        self.run_check()
        self.restart()
        self.bit.close_profile_verified.return_value=True
        self.run_check()
        self.bit.open_profile_for_check.assert_called_once()
        self.cloud.observe_publication.assert_not_called()
        self.assertEqual(self.cloud.release_publication_check.call_args.args[2]['profile_disposition'],'closed')
        self.assertEqual(self.state()['phase'],'settled')
    def test_open_response_lost_never_opens_again_or_guesses_closed(self):
        def lost(profile, *, before_open=lambda:None):
            before_open();raise TimeoutError('private lost reply')
        self.bit.open_profile_for_check.side_effect=lost
        self.run_check();self.restart();self.run_check()
        self.bit.open_profile_for_check.assert_called_once()
        self.bit.close_profile_verified.assert_not_called()
        self.cloud.observe_publication.assert_not_called()
        self.cloud.release_publication_check.assert_not_called()
        self.assertEqual(self.state()['phase'],'possible_open')
    def test_claim_response_lost_settles_same_key_across_session_and_restart(self):
        self.cloud.claim_publication_check.side_effect=TimeoutError('lost')
        self.cloud.settle_unreceived_publication_check.side_effect=TimeoutError('settlement lost')
        with self.assertRaises(TimeoutError):self.run_check()
        key=self.state()['request_key']
        self.restart()
        self.cloud.settle_unreceived_publication_check.side_effect=None
        self.cloud.settle_unreceived_publication_check.return_value=dict(request_key=key,reservation_state='cancelled')
        self.job.tick('new-same-owner-token','9',current=lambda:True)
        self.assertEqual(self.cloud.settle_unreceived_publication_check.call_args.args,('new-same-owner-token',key))
        self.cloud.claim_publication_check.assert_called_once()
        self.bit.open_profile_for_check.assert_not_called()
    def test_other_owner_never_recovers_original_scope(self):
        self.bit.close_profile_verified.return_value=False
        self.run_check()
        original=self.state()
        self.job.tick('other-token','10',current=lambda:False)
        self.assertEqual(self.state(),original)
    def test_late_active_after_local_settlement_cannot_open(self):
        entered=Event();reply=Event()
        def claim(*args,**kwargs):
            entered.set();reply.wait(3);return self.claim
        self.cloud.claim_publication_check.side_effect=claim
        worker=Thread(target=self.run_check);worker.start();self.assertTrue(entered.wait(2))
        state=self.state()
        self.assertTrue(self.job.records.transition('dev','9',state['request_key'],'claiming','settling'))
        reply.set();worker.join(3)
        self.bit.open_profile_for_check.assert_not_called()
        self.assertEqual(self.state()['phase'],'settling')
        self.run_check()
        self.assertEqual(self.state()['phase'],'settled')
    def test_released_ack_never_reopens_and_snapshot_is_not_required(self):
        self.cloud.claim_publication_check.return_value=dict(self.ack,reservation_state='released',replayed=True)
        self.cloud.settle_unreceived_publication_check.side_effect=None
        self.cloud.settle_unreceived_publication_check.return_value=dict(self.ack,reservation_state='released',replayed=True)
        self.run_check()
        self.bit.open_profile_for_check.assert_not_called()
        self.assertEqual(self.state()['phase'],'settled')
    def test_secret_fields_do_not_enter_sqlite(self):
        self.claim['token']='secret-token'
        self.claim['attempt']['input_snapshot']['file_url']='https://signed.invalid/secret'
        self.claim['attempt']['input_snapshot']['content']['Cookie']='secret-cookie'
        self.bit.close_profile_verified.return_value=False
        self.run_check()
        self.assertNotIn('secret',str(self.state()))
        self.assertNotIn('Cookie',str(self.state()))
    def test_api_only_check_has_not_opened_proof(self):
        self.checker._read_api=Mock(return_value=PublicWorkObservation('uncertain','missing','original account read complete',lookup_result='not_found'))
        self.run_check()
        self.bit.open_profile_for_check.assert_not_called()
        self.assertEqual(self.cloud.observe_publication.call_args.args[2]['profile_disposition'],'not_opened')
    def test_no_persistence_is_rejected_at_construction(self):
        with self.assertRaises(AttributeError):
            PublicationCheckJob(self.cloud,self.bit,SimpleNamespace(profile_operations=ProfileOperations(),profile_in_use=lambda _:False),lambda _:None,cloud_env='dev')

    def test_possible_open_is_durable_before_external_open(self):
        def opening(profile, *, before_open=lambda:None):
            before_open()
            self.assertEqual(self.state()['phase'],'possible_open')
            return '127.0.0.1:9999'
        self.bit.open_profile_for_check.side_effect=opening
        self.run_check()
        self.assertEqual(self.state()['phase'],'settled')
    def test_closed_packet_is_durable_before_ack_and_does_not_store_token(self):
        def observe(token,task,packet):
            self.assertEqual(self.state()['phase'],'packet')
            self.assertEqual(self.state()['packet'],packet)
            self.assertNotIn(token,str(self.state()))
            return self.ack
        self.cloud.observe_publication.side_effect=observe
        self.run_check()
        self.assertEqual(self.state()['phase'],'settled')
    def test_active_restart_revalidates_original_session_and_preserves_snapshot(self):
        self.journal.profile_operations.occupied=lambda _:True
        self.job.publication_occupied=self.journal.profile_operations.occupied
        self.run_check()
        key=self.state()['request_key']
        self.journal.profile_operations.occupied=lambda _:False
        self.restart()
        self.job.tick('token','9',current=lambda:True)
        self.assertEqual(self.cloud.claim_publication_check.call_count,2)
        self.assertEqual(self.state()['request_key'],key)
        self.bit.open_profile_for_check.assert_called_once()
        reference=self.reader.read.call_args.args[0]
        self.assertEqual(reference.platform_uid,self.source['platform_uid'])
        self.assertEqual(reference.submission_id,self.claim['task']['platform_submission_id'])
    def test_stale_observation_switches_to_release_without_reopening(self):
        from wt_media_agent.clients.cloud.errors import CloudError
        self.cloud.observe_publication.side_effect=CloudError('publication_conflict','revision changed')
        self.run_check()
        self.assertTrue(self.state()['packet'].get('release_only'))
        self.run_check()
        self.cloud.release_publication_check.assert_called_once()
        self.bit.open_profile_for_check.assert_called_once()
    def test_denied_admission_does_not_rotate_key_and_recovery_ignores_guard(self):
        from contextlib import contextmanager
        @contextmanager
        def denied():yield False
        self.job.admission_guard=denied
        self.run_check()
        self.cloud.claim_publication_check.assert_not_called()
        self.assertIsNone(self.state())
        state=self.job.records.begin('dev','9',{})
        self.job.records.transition('dev','9',state['request_key'],'claiming','settling')
        self.run_check()
        self.assertEqual(self.state()['phase'],'settled')
        self.assertEqual(self.state()['request_key'],state['request_key'])
        self.run_check()
        self.assertEqual(self.state()['request_key'],state['request_key'])
    def test_admission_guard_covers_claim_and_accept_but_not_read_or_observe(self):
        from contextlib import contextmanager
        guarded=[False]
        @contextmanager
        def guard():
            guarded[0]=True
            try:yield True
            finally:guarded[0]=False
        self.job.admission_guard=guard
        def claim(*args,**kwargs):
            self.assertTrue(guarded[0]);return self.claim
        self.cloud.claim_publication_check.side_effect=claim
        original=self.reader.read.return_value
        def reading(*args,**kwargs):
            self.assertFalse(guarded[0]);return original
        self.reader.read.side_effect=reading
        self.run_check()
        self.assertEqual(self.state()['phase'],'settled')

    def test_new_session_active_replay_rejection_only_settles_without_platform_read(self):
        from wt_media_agent.clients.cloud.errors import CloudError
        self.job.publication_occupied=lambda _:True
        self.run_check()
        original_key=self.state()['request_key']
        self.job.publication_occupied=lambda _:False
        self.restart()
        self.cloud.claim_publication_check.side_effect=CloudError('publication_profile_use_conflict','old session refused')
        self.job.tick('new-same-owner-token','9',current=lambda:True)
        self.bit.open_profile_for_check.assert_not_called();self.reader.read.assert_not_called()
        self.cloud.observe_publication.assert_not_called()
        self.assertEqual(self.cloud.claim_publication_check.call_args.args,('new-same-owner-token',original_key))
        self.assertTrue(self.cloud.release_publication_check.call_args.args[2]['release_only'])
    def test_revoked_active_snapshot_never_opens_or_checks_platform(self):
        self.job.publication_occupied=lambda _:True
        self.run_check()
        self.restart()
        self.claim['task']['revision']='8'
        self.claim['expected_revision']='8'
        self.run_check()
        self.bit.open_profile_for_check.assert_not_called();self.reader.read.assert_not_called()
        self.cloud.observe_publication.assert_not_called()
        self.cloud.release_publication_check.assert_called_once()
