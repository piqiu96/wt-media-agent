"""Public-work proof checks use fixtures, never claim platform acceptance."""
from datetime import datetime,timezone
import unittest
from wt_media_agent.platforms.baidu.publication_check import (
    is_exact_target_url, read_public_work_id, verify_public_metadata,
)

TARGET='https://baijiahao.baidu.com/builder/preview/s?id=1882371730190912814'
PUBLIC='https://mbd.baidu.com/newspage/data/videolanding?nid=sv_7000395122085014122'
class PublicProofTests(unittest.TestCase):
    def test_receipt_target_and_public_work_are_distinct_exact_identities(self):
        self.assertTrue(is_exact_target_url('1882371730190912814',TARGET))
        self.assertFalse(is_exact_target_url('7000395122085014122',TARGET))
        self.assertEqual(read_public_work_id(PUBLIC),'7000395122085014122')
        for raw in [PUBLIC+'&token=x',PUBLIC.replace('https:','http:'),PUBLIC.replace('mbd.baidu.com','mbd.baidu.com.evil.test'),PUBLIC+'#changed',TARGET]:
            self.assertEqual(read_public_work_id(raw),'')
    def test_only_matching_anonymous_metadata_and_loaded_media_prove_public_work(self):
        metadata={'id':'7000395122085014122','title':'确认标题 #话题','publish_time':1795169573,'duration':30,'media_ready':True}
        now=datetime(2026,11,21,tzinfo=timezone.utc)
        at=verify_public_metadata(PUBLIC,'确认标题 #话题',metadata,now=now)
        self.assertEqual(at,datetime(2026,11,20,10,12,53,tzinfo=timezone.utc))
        for change in [{'id':'other'},{'id':7000395122085014122},{'title':'另一作品'},{'media_ready':False},{'publish_time':None},{'publish_time':now.timestamp()+60},{'publish_time':True},{'duration':0}]:
            with self.subTest(change=change):
                self.assertIsNone(verify_public_metadata(PUBLIC,'确认标题 #话题',dict(metadata,**change),now=now))
