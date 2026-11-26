import copy
import unittest
from unittest.mock import Mock, patch

from wt_media_agent.clients.bilibili.metrics import MetricsError, read_metrics
from wt_media_agent.clients.cloud import CloudAgentClient, CloudUnavailableError
from wt_media_agent.runner.work_metrics import WorkMetricsLoop


def payload():
    return {'code': 0, 'data': {'aid': 123, 'owner': {'mid': 456}, 'stat': {
        'aid': 123, 'view': 7, 'like': 1, 'favorite': 2, 'coin': 0, 'reply': 0, 'danmaku': 0}}}


class MetricsTest(unittest.TestCase):
    def test_exact_identity_and_real_zero(self):
        counts = read_metrics('123', '456', fetch=lambda aid: payload())
        self.assertEqual(counts, {'view': 7, 'like': 1, 'favorite': 2, 'coin': 0, 'reply': 0, 'danmaku': 0})

    def test_missing_bad_values_and_foreign_identity_are_not_zero(self):
        for key, value in [('view', None), ('like', True), ('coin', -1), ('reply', '0'), ('danmaku', 9007199254740992)]:
            body = payload();body['data']['stat'][key] = value
            with self.subTest(key=key), self.assertRaises(MetricsError):
                read_metrics('123', '456', fetch=lambda aid: body)
        for aid, uid in [('124', '456'), ('123', '457'), ('00123', '456'), ('https://example.com', '456')]:
            with self.assertRaises(MetricsError):
                read_metrics(aid, uid, fetch=lambda aid: payload())
        for code in [-412, -509]:
            with self.assertRaises(MetricsError) as caught:
                read_metrics('123', '456', fetch=lambda aid: {'code': code})
            self.assertEqual(caught.exception.code, 'rate_limited')

    def test_cloud_claim_validates_identity_without_platform_urls(self):
        claim = {'work_id': '16', 'platform_work_id': '123', 'platform_account_uid': '456', 'lease_token': 'a' * 64}
        transport = Mock(return_value={'data': claim})
        client = CloudAgentClient('http://cloud.test', transport=transport)
        self.assertEqual(client.claim_work_metrics('token'), claim)
        bad = copy.copy(claim);bad['work_id'] = '../1'
        transport.return_value = {'data': bad}
        with self.assertRaises(CloudUnavailableError): client.claim_work_metrics('token')

    @patch('wt_media_agent.runner.work_metrics.routing_user_id', return_value='9')
    def test_loop_reports_safe_failure_and_uses_original_claim(self, _):
        cloud = Mock()
        cloud.claim_work_metrics.return_value = {'work_id': '16', 'platform_work_id': '123', 'platform_account_uid': '456', 'lease_token': 'a' * 64}
        token = Mock();token.get.return_value = 'token'
        collect = Mock(side_effect=MetricsError('rate_limited'))
        loop = WorkMetricsLoop(cloud, token, collect=collect)
        loop.tick()
        args = cloud.report_work_metrics.call_args.args
        self.assertEqual(args[:2], ('token', '16'))
        self.assertNotIn('counts', args[2]);self.assertEqual(args[2]['error'], 'rate_limited')
        collect.assert_called_once_with('123', '456')
        cloud.reset_mock();token.get.side_effect = ['token', 'changed']
        loop.tick();cloud.report_work_metrics.assert_not_called()
