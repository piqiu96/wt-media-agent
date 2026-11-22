"""Profile-bound identity reads never export cookies or use a local HTTP client."""
import unittest
from unittest.mock import MagicMock

from wt_media_agent.platforms.baidu.identity import read_baidu_identity, _identity_request_only


class BaiduBrowserIdentityTests(unittest.TestCase):
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
