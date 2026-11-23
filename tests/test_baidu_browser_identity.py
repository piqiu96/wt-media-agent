"""Profile-bound identity reads never export cookies or use a local HTTP client."""
import unittest
import json
from unittest.mock import MagicMock, patch

from wt_media_agent.platforms.baidu.identity import read_baidu_identity, _identity_request_only
from wt_media_agent.clients.baidu.identity import identify_baidu
from wt_media_agent.clients.baidu.publication import BaiduPublicationClient


class BaiduBrowserIdentityTests(unittest.TestCase):
    def test_identity_channels_agree_on_observed_shapes_and_invalid_uids(self):
        for status, uid, expected in [
            (None, 6572476037, 'normal'),
            ({'code': 0}, '6572476037', 'normal'),
            ({'code': 1}, '6572476037', 'expired'),
            (None, True, 'not_logged_in'),
            (None, '06572476037', 'not_logged_in'),
            (None, 'foreign', 'not_logged_in'),
        ]:
            with self.subTest(status=status, uid=uid):
                body = {'data': {'user': {'user_id': uid}}}
                if status is not None:
                    body['status'] = status
                factory = MagicMock()
                page = factory.return_value.__enter__.return_value.page
                page.goto.return_value.status = 200
                page.goto.return_value.json.return_value = body
                browser = read_baidu_identity('127.0.0.1:9999', browser_session_factory=factory)
                session = MagicMock()
                session.get_json.return_value = {'status': 200, 'json': body}
                client = BaiduPublicationClient({}, session_factory=lambda config, **policy: session)
                native = client.read_identity()
                client.close()
                with patch('wt_media_agent.clients.baidu.identity.urlrequest.urlopen') as request:
                    request.return_value.read.return_value = json.dumps(body).encode()
                    legacy = identify_baidu([{'name': 'BDUSS', 'value': 'fixture-secret'}])
                for result in (browser, native, legacy):
                    self.assertEqual(result['login_status'], expected)
                    self.assertEqual(result['platform_account_id'], '6572476037' if expected == 'normal' else '')

    def test_platform_refusal_with_stale_uid_is_not_a_logged_in_identity(self):
        factory=MagicMock()
        page=factory.return_value.__enter__.return_value.page
        page.goto.return_value.status=200
        page.goto.return_value.json.return_value={'status':{'code':1,'msg':'expired'},'data':{'user':{'user_id':'6572476037'}}}
        result=read_baidu_identity('127.0.0.1:9999',browser_session_factory=factory)
        self.assertNotEqual(result['login_status'],'normal')
        self.assertEqual(result['platform_account_id'],'')
        page.close.assert_called_once()
    def test_reads_owned_browser_and_closes_only_its_new_tab(self):
        factory=MagicMock()
        page=factory.return_value.__enter__.return_value.page
        page.goto.return_value.status=200
        page.goto.return_value.json.return_value={'data':{'user':{'user_id':6572476037}}}
        self.assertEqual(read_baidu_identity('127.0.0.1:9999',browser_session_factory=factory),
                         {'login_status':'normal','platform_account_id':'6572476037'})
        page.goto.assert_called_once()
        self.assertTrue(page.goto.call_args.args[0].startswith('https://image.baidu.com/user/logininfo?'))
        page.close.assert_called_once()

    def test_navigation_failure_still_closes_identity_tab(self):
        factory=MagicMock();page=factory.return_value.__enter__.return_value.page
        page.goto.side_effect=ConnectionError('fixture')
        with self.assertRaises(ConnectionError):read_baidu_identity('127.0.0.1:9999',browser_session_factory=factory)
        page.close.assert_called_once()

    def test_identity_tab_cannot_make_posts_or_follow_another_origin(self):
        for method,url,allowed in [('GET','https://image.baidu.com/user/logininfo?time=1',True),
                                   ('POST','https://image.baidu.com/user/logininfo',False),
                                   ('GET','https://other.test/user/logininfo',False)]:
            route=MagicMock();route.request.method=method;route.request.url=url
            _identity_request_only(route)
            self.assertEqual(route.fallback.call_count,int(allowed))
            self.assertEqual(route.abort.call_count,int(not allowed))
