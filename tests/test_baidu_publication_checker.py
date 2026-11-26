"""Platform policy belongs to one checker, independent of task storage/Cloud."""
import unittest
from contextlib import nullcontext
from unittest.mock import Mock
from wt_media_agent.platforms.baidu import publication_check as baidu
from wt_media_agent.services.browser.http import ProfileHttpError


class BaiduPublicationCheckerTests(unittest.TestCase):
    def checker(self, *, fallback=True):
        self.assertTrue(hasattr(baidu, 'BaiduPublicationChecker'), 'missing complete checker capability')
        from wt_media_agent.platforms.publication_check import PublicationReference, PublicationCheckControl
        self.client = Mock()
        self.client.read_identity.return_value = {'login_status':'normal', 'platform_account_id':'9'}
        self.client.find_submission.return_value = {'article_id':'123', 'status':'review', 'is_published':0}
        self.browser = Mock(side_effect=lambda:nullcontext('127.0.0.1:1234'))
        self.control = PublicationCheckControl(current=lambda:True, browser_fallback_allowed=fallback,
            read_environment=lambda:{}, browser=self.browser)
        self.reference = PublicationReference('9', '123', {'description':'title'})
        return baidu.BaiduPublicationChecker(client_factory=lambda env:self.client)

    def test_pending_api_observation_does_not_open_browser(self):
        checker = self.checker()
        self.assertEqual(checker.check(self.reference, self.control).conclusion, 'reviewing')
        self.browser.assert_not_called()
        self.client.close.assert_called_once()

    def test_foreign_uid_blocks_before_submission_query(self):
        checker = self.checker()
        self.client.read_identity.return_value['platform_account_id'] = '10'
        self.assertEqual(checker.check(self.reference, self.control).lookup_result, 'blocked')
        self.client.find_submission.assert_not_called()
        self.browser.assert_not_called()

    def test_identity_fallback_diagnostic_does_not_log_account_details(self):
        checker = self.checker(fallback=False)
        self.client.read_identity.return_value = {'login_status':'expired', 'platform_account_id':'private-secret'}
        with self.assertLogs(baidu.logger, level='INFO') as logs:
            checker.check(self.reference, self.control)
        self.assertIn('api_fallback_reason=identity', ' '.join(logs.output))
        self.assertNotIn('private-secret', ' '.join(logs.output))

    def test_exhausted_fallback_does_not_open_browser(self):
        checker = self.checker(fallback=False)
        self.client.read_identity.return_value['login_status'] = 'expired'
        self.assertEqual(checker.check(self.reference, self.control).lookup_result, 'blocked')
        self.browser.assert_not_called()

    def test_public_proof_can_use_browser_but_incomplete_proof_stops_reopening(self):
        from wt_media_agent.platforms.publication_check import PublicWorkObservation
        checker = self.checker(fallback=False)
        self.client.find_submission.return_value.update(status='publish', is_published=1)
        checker._read_browser = Mock(return_value=PublicWorkObservation('reviewing','pending','incomplete'))
        self.assertEqual(checker.check(self.reference, self.control).lookup_result, 'blocked')
        self.browser.assert_called_once()
        self.client.close.assert_called_once()

    def test_transport_failure_is_deferred_without_browser_or_secret_text(self):
        checker = self.checker()
        self.client.read_identity.side_effect = ProfileHttpError('private')
        with self.assertRaises(ProfileHttpError):
            checker.check(self.reference, self.control)
        self.browser.assert_not_called()
        self.client.close.assert_called_once()
