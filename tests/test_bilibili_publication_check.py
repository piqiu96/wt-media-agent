from contextlib import contextmanager
from datetime import datetime, timezone
import unittest

from wt_media_agent.platforms.bilibili.publication_check import BilibiliPublicationChecker, verify_metadata
from wt_media_agent.platforms.publication_check import PublicationReference, PublicationCheckControl, PublicationCheckError


class BilibiliCheckTest(unittest.TestCase):
    def setUp(self):
        self.reference = PublicationReference('456', '123', {'title': '画中画演示', 'bilibili': {'visibility': 'public'}})
        self.row = {'aid': 123, 'mid': 456, 'title': '画中画演示', 'state': -30,
                    'state_desc': '审核中', 'attrs': {'is_owner': 1, 'no_public': 0}}
        self.closed = False
        self.browser_opened = False
        test = self
        class Client:
            def __init__(self, _): pass
            def read_identity(self): return {'login_status': 'normal', 'platform_account_id': '456'}
            def find_submission(self, aid, *, current):
                test.assertEqual(aid, '123')
                return test.row
            def close(self): test.closed = True
        @contextmanager
        def browser():
            self.browser_opened = True
            raise AssertionError('non-public original must not open browser')
            yield
        self.checker = BilibiliPublicationChecker(client_factory=Client)
        self.control = PublicationCheckControl(lambda: True, False, lambda: {}, browser)

    def test_reviewing_is_exact_original_and_never_published(self):
        result = self.checker.check(self.reference, self.control)
        self.assertEqual(result.conclusion, 'reviewing')
        self.assertEqual(result.submission_id, '123')
        self.assertEqual(result.target_url, 'https://www.bilibili.com/video/av123')
        self.assertTrue(self.closed)
        self.assertFalse(self.browser_opened)

    def test_rejection_is_blocked_not_reviewing_or_retry(self):
        self.row.update(state=-4, state_desc='未通过', reject_reason='内容需修改')
        result = self.checker.check(self.reference, self.control)
        self.assertEqual((result.conclusion, result.lookup_result), ('uncertain', 'blocked'))
        self.assertIn('内容需修改', result.basis)

    def test_missing_original_retains_uncertain(self):
        self.row = None
        result = self.checker.check(self.reference, self.control)
        self.assertEqual((result.conclusion, result.lookup_result), ('uncertain', 'not_found'))

    def test_private_archive_never_counts_as_public(self):
        self.row.update(state=0, state_desc='开放浏览', attrs={'is_owner': 1, 'no_public': 1})
        self.assertEqual(self.checker.check(self.reference, self.control).lookup_result, 'blocked')

    def test_changed_owner_or_content_cannot_bind(self):
        for changes in ({'mid': 999}, {'title': '别人的作品'}):
            with self.subTest(changes=changes):
                old = self.row.copy()
                self.row.update(changes)
                with self.assertRaises(PublicationCheckError):
                    self.checker.check(self.reference, self.control)
                self.row = old

    def test_anonymous_metadata_requires_exact_identity_and_actual_public_time(self):
        now = datetime(2026, 11, 26, tzinfo=timezone.utc)
        data = {'aid': 123, 'owner': {'mid': 456}, 'title': '画中画演示', 'state': 0,
                'pubdate': int(now.timestamp()) - 60, 'duration': 41.4, 'cid': 777, 'bvid': 'BV135WM6uE4A'}
        self.assertIsNotNone(verify_metadata(self.reference, data, now=now))
        for change in ({'aid': 124}, {'owner': {'mid': 999}}, {'title': '替代作品'},
                       {'state': -30}, {'state': False}, {'cid': 0}, {'duration': float('nan')},
                       {'pubdate': int(now.timestamp()) + 60}, {'pubdate': 0}):
            with self.subTest(change=change):
                self.assertIsNone(verify_metadata(self.reference, {**data, **change}, now=now))


if __name__ == '__main__': unittest.main()
