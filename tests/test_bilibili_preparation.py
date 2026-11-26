"""SQLite preparation/evidence checks; these are not real-platform acceptance."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_publication_preparation import FakeCloud, FakeBit, cloud_claim
from wt_media_agent.platforms.publish import PublicationInputError
from wt_media_agent.runner.publication_preparation import PublicationPreparation
from wt_media_agent.storage.database import Database
from wt_media_agent.storage.migration import apply_migrations
from wt_media_agent.storage.publication import PublicationJournal, PublicationClaimStore


def bilibili_claim():
    claim = cloud_claim()
    claim['task']['platform'] = 'bilibili'
    source = claim['attempt']['input_snapshot']
    source.update(platform='bilibili', portrait_cover_sha256='', portrait_cover_size=0,
                  space_cover_sha256='d' * 64, space_cover_size=12)
    source['content'] = dict(title='独立发布标题', description='独立简介', cover_file_id='5',
        space_cover_file_id='7', portrait_cover_file_id='0', topics='', declaration='', confirmed=True,
        bilibili=dict(partition_id=17, partition_name='游戏', tags=['游戏'],
                     statement='内容含营销信息', visibility='public', rights_reserved=False))
    claim['files'].pop('portrait_cover')
    claim['files']['space_cover'] = dict(url='https://storage.test/space', sha256='d'*64, size=12)
    return claim


class BilibiliPreparationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        db = self.root / 'agent.sqlite3'
        apply_migrations(db)
        self.journal = PublicationJournal(Database(db))
        self.cloud, self.bit = FakeCloud(), FakeBit()
        self.cloud.value = bilibili_claim()
        self.prep = PublicationPreparation(self.cloud, self.bit, self.journal,
            PublicationClaimStore(Database(db)), self.root / 'files', cloud_env='dev')

    def test_bilibili_journal_freezes_title_options_and_space_cover(self):
        claim, identity = self.prep.claim('in-memory-token', '3')
        saved = self.journal.get(identity)['input']
        self.assertEqual(saved['platform'], 'bilibili')
        self.assertEqual(saved['content']['title'], '独立发布标题')
        self.assertEqual(saved['content']['bilibili']['tags'], ['游戏'])
        self.assertEqual(saved['space_cover_sha256'], 'd'*64)
        self.assertNotIn('portrait_cover_sha256', saved)
        self.assertNotIn('files', saved)
        claim['attempt']['input_snapshot']['content']['bilibili']['tags'][0] = 'changed'
        self.assertEqual(self.journal.get(identity)['input']['content']['bilibili']['tags'], ['游戏'])
        self.assertEqual(self.bit.scans, 0)

    def test_downloads_and_ready_evidence_use_only_the_platform_secondary_cover(self):
        claim, identity = self.prep.claim('in-memory-token', '3')
        downloads = []
        def download(grant, target, **kwargs):
            downloads.append(target.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'controlled-fixture')
        with patch('wt_media_agent.runner.publication_preparation.download_verified', download):
            prepared = self.prep.prepare('in-memory-token', claim, identity)
        self.assertEqual(downloads, ['cover', 'space-cover', 'video.mp4'])
        self.assertIsNone(prepared.portrait_cover)
        self.assertEqual(prepared.space_cover.name, 'space-cover')
        self.prep.mark_ready('in-memory-token', identity)
        report = self.cloud.reports[-1][2]
        self.assertEqual(report['space_cover_sha256'], 'd'*64)
        self.assertNotIn('portrait_cover_sha256', report)

    def test_changed_secondary_grant_or_cross_platform_snapshot_never_opens_browser(self):
        claim, identity = self.prep.claim('in-memory-token', '3')
        claim['files']['space_cover']['sha256'] = 'e'*64
        with patch.object(self.bit, 'open_profile_with_devtools') as opened:
            with self.assertRaises(PublicationInputError):
                self.prep.prepare('in-memory-token', claim, identity)
            opened.assert_not_called()
        self.cloud.value = bilibili_claim()
        self.cloud.value['attempt']['input_snapshot']['platform'] = 'baidu'
        with self.assertRaises(PublicationInputError):
            self.prep.claim('in-memory-token', '3')
