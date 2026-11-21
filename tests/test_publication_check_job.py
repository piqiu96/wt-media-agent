import copy
import unittest
from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import Mock

from wt_media_agent.platforms.publish import PublicWorkObservation
from wt_media_agent.runner.publication_check import PublicationCheckJob
from wt_media_agent.runtime.profile_operation import ProfileOperations

TARGET='https://baijiahao.baidu.com/builder/preview/s?id=1882371730190912814'
PUBLIC='https://mbd.baidu.com/newspage/data/videolanding?nid=sv_7000395122085014122'
class PublicCheckJobTests(unittest.TestCase):
    def setUp(self):
        self.source={'profile_id':'37','bit_profile_id':'original-profile','bit_main_user_id':'bit-owner','platform_uid':'6572476037','content':{'description':'确认标题','topics':'#话题','declaration':'无需声明'}}
        self.claim={'generation':'3','expected_revision':'7','task':{'id':'9007199254740993','operator_id':'9','status':'reviewing','platform':'baidu','revision':'7','current_attempt_id':'9007199254740995','platform_account_uid':'6572476037','platform_submission_id':'1882371730190912814','target_url':None},'attempt':{'id':'9007199254740995','publication_id':'9007199254740993','executor_user_id':'9','profile_id':'37','input_snapshot':self.source}}
        self.cloud=Mock();self.cloud.claim_publication_check.return_value=self.claim
        self.bit=Mock();self.bit.scan_profiles.return_value=SimpleNamespace(profiles=[SimpleNamespace(bit_profile_id='original-profile',main_user_id='bit-owner')])
        self.bit.open_profile_for_check.return_value='127.0.0.1:9999'
        self.bit.close_profile_verified.return_value=True
        self.reader=Mock();self.reader.read.return_value=PublicWorkObservation('published','指定作品已经公开','原账号、指定原链接、匿名媒体、确认内容和平台时间均已匹配','1882371730190912814',TARGET,'7000395122085014122',PUBLIC,datetime(2026,11,20,10,12,53,tzinfo=timezone.utc))
        self.journal=SimpleNamespace(profile_operations=ProfileOperations())
        self.job=PublicationCheckJob(self.cloud,self.bit,self.journal,lambda platform:lambda endpoint:self.reader)
    def test_public_check_never_registers_attempt_or_downloads_or_publishes(self):
        original=copy.deepcopy(self.claim)
        self.job.tick('token','9',current=lambda:True)
        args=self.cloud.observe_publication.call_args.args
        self.assertEqual(args[:2],('token','9007199254740993'))
        self.assertEqual(args[2]['generation'],'3')
        observation=args[2]['observation']
        self.assertEqual((observation['attempt_id'],observation['expected_revision']),('9007199254740995','7'))
        self.assertEqual(observation['conclusion'],'published')
        self.assertEqual(observation['published_at'],'2026-11-20T10:12:53Z')
        self.assertEqual(self.claim,original)
        self.reader.read.assert_called_once()
        self.bit.close_profile_verified.assert_called_once_with('original-profile','127.0.0.1:9999')
    def test_missing_proof_or_close_failure_preserves_unknown_without_public_fields(self):
        self.bit.close_profile_verified.return_value=False
        self.job.tick('token','9',current=lambda:True)
        observation=self.cloud.observe_publication.call_args.args[2]['observation']
        self.assertEqual(observation['conclusion'],'uncertain')
        self.assertNotIn('work_url',observation)
        self.assertNotIn('published_at',observation)
    def test_current_identity_change_never_reports_or_starts_another_profile(self):
        self.job.tick('token','9',current=lambda:False)
        self.cloud.claim_publication_check.assert_not_called()
        self.bit.open_profile_for_check.assert_not_called()
        self.cloud.observe_publication.assert_not_called()
    def test_foreign_or_malformed_claim_is_rejected_before_browser(self):
        self.claim['attempt']['executor_user_id']='10'
        with self.assertRaises(ValueError):self.job.tick('token','9',current=lambda:True)
        self.bit.open_profile_for_check.assert_not_called()
        self.cloud.observe_publication.assert_not_called()
    def test_unresolved_profile_is_not_opened_or_closed(self):
        self.journal.profile_operations.occupied=lambda profile:True
        self.job.tick('token','9',current=lambda:True)
        self.bit.open_profile_for_check.assert_not_called()
        self.bit.close_profile_verified.assert_not_called()
        self.assertEqual(self.cloud.observe_publication.call_args.args[2]['observation']['conclusion'],'uncertain')

    def test_identity_change_during_read_closes_owned_scene_without_report(self):
        active=[True]
        def read(*_,**__):
            active[0]=False
            return PublicWorkObservation('uncertain','账号发生变化','本轮核验中当前用户已经发生变化，不能回报原账号的结果')
        self.reader.read.side_effect=read
        self.job.tick('token','9',current=lambda:active[0])
        self.bit.close_profile_verified.assert_called_once()
        self.cloud.observe_publication.assert_not_called()

    def test_network_failure_preserves_original_and_reports_only_unknown(self):
        self.reader.read.side_effect=TimeoutError('private platform response')
        original=copy.deepcopy(self.claim)
        self.job.tick('token','9',current=lambda:True)
        value=self.cloud.observe_publication.call_args.args[2]['observation']
        self.assertEqual(value['conclusion'],'uncertain')
        self.assertTrue(value['evidence']['scene_closed'])
        self.assertNotIn('work_url',value)
        self.assertNotIn('private platform response',str(value))
        self.assertEqual(self.claim,original)

    def test_lost_observation_reply_never_replays_a_publication(self):
        original=copy.deepcopy(self.claim)
        self.cloud.claim_publication_check.side_effect=[self.claim,None]
        self.cloud.observe_publication.side_effect=TimeoutError('reply lost')
        with self.assertRaises(TimeoutError):
            self.job.tick('token','9',current=lambda:True)
        self.job.tick('token','9',current=lambda:True)
        self.reader.read.assert_called_once()
        self.bit.open_profile_for_check.assert_called_once()
        self.cloud.observe_publication.assert_called_once()
        self.assertEqual(self.claim,original)
