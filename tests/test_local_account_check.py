from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wt_media_agent.local_api.server import LocalApiServer
from wt_media_agent.runtimes.bitbrowser import BitBrowserResponseError


class CookieClient:
    def __init__(self, cookies: list[dict[str, object]]) -> None:
        self.cookies = cookies
        self.opened: list[str] = []

    def open_profile(self, profile_id: str) -> None:
        self.opened.append(profile_id)

    def read_cookies(self, profile_id: str) -> list[dict[str, object]]:
        return self.cookies

    def scan_profiles(self):
        raise AssertionError("not used")

    def create_profile(self, config: dict[str, object]) -> str:
        raise AssertionError("not used")

    def close_profile(self, profile_id: str) -> None:
        raise AssertionError("not used")

    def update_profile(self, profile_id: str, config: dict[str, object]) -> None:
        raise AssertionError("not used")

    def delete_profile(self, profile_id: str) -> None:
        raise AssertionError("not used")


class ErrorClient(CookieClient):
    def open_profile(self, profile_id: str) -> None:
        raise BitBrowserResponseError("upstream unavailable")


class LocalAccountCheckTests(unittest.TestCase):
    def test_bilibili_identifies_via_nav_api_without_leaking_cookie(self) -> None:
        import json as _json
        from unittest import mock

        fake_body = _json.dumps({
            "code": 0, "message": "0",
            "data": {"mid": 293793435, "uname": "测试用户", "face": "http://avatar", "isLogin": True},
        })
        server = LocalApiServer(bitbrowser=CookieClient([{"name": "SESSDATA", "value": "secret-session"}]))

        with mock.patch("wt_media_agent.local_api.server.urlrequest.urlopen") as urlopen:
            urlopen.return_value.read.return_value = fake_body.encode()
            status, payload = server.account_check_response({"profile_id": "profile-1", "platform": "bilibili"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["platform_account_id"], "293793435")
        self.assertEqual(payload["data"]["name"], "测试用户")
        self.assertEqual(payload["data"]["login_status"], "normal")
        self.assertNotIn("secret-session", str(payload))

    def test_expected_uid_mismatch_returns_account_mismatch(self) -> None:
        import json as _json
        from unittest import mock

        fake_body = _json.dumps({"code": 0, "message": "0", "data": {"mid": 293793435, "uname": "u", "face": "", "isLogin": True}})
        server = LocalApiServer(bitbrowser=CookieClient([{"name": "SESSDATA", "value": "s"}]))
        with mock.patch("wt_media_agent.local_api.server.urlrequest.urlopen") as urlopen:
            urlopen.return_value.read.return_value = fake_body.encode()
            status, payload = server.account_check_response({
                "profile_id": "profile-1",
                "platform": "bilibili",
                "expected_platform_account_id": "999",
            })

        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["login_status"], "account_mismatch")

    def test_unknown_platform_identity_is_not_success(self) -> None:
        server = LocalApiServer(bitbrowser=CookieClient([{"name": "passport_csrf_token", "value": "secret"}]))

        status, payload = server.account_check_response({"profile_id": "profile-1", "platform": "douyin"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["platform_account_id"], "")
        self.assertEqual(payload["data"]["login_status"], "environment_error")

    def test_baijiahao_identifies_via_logininfo_api(self) -> None:
        import json as _json
        from unittest import mock

        fake_body = _json.dumps({
            "status": {"code": 0, "msg": ""},
            "data": {"user": {"user_id": 6572476037, "user_name": "你阿邱爷", "portrait": "abc123", "is_login": 1}},
        })
        server = LocalApiServer(bitbrowser=CookieClient([{"name": "BDUSS", "value": "real-bduss"}]))

        with mock.patch("wt_media_agent.local_api.server.urlrequest.urlopen") as urlopen:
            urlopen.return_value.read.return_value = fake_body.encode()
            status, payload = server.account_check_response({"profile_id": "profile-1", "platform": "baijiahao"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["platform_account_id"], "6572476037")
        self.assertEqual(payload["data"]["name"], "你阿邱爷")
        self.assertEqual(payload["data"]["login_status"], "normal")
        self.assertIn("avatar_url", payload["data"])

    def test_baijiahao_without_bduss_is_not_logged_in(self) -> None:
        server = LocalApiServer(bitbrowser=CookieClient([{"name": "PSTM", "value": "17"}]))
        from unittest import mock
        with mock.patch("wt_media_agent.local_api.server.urlrequest.urlopen") as urlopen:
            status, payload = server.account_check_response({"profile_id": "profile-1", "platform": "baijiahao"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["login_status"], "not_logged_in")
        urlopen.assert_not_called()

    def test_bitbrowser_failure_is_mapped_without_cookie_payload(self) -> None:
        server = LocalApiServer(bitbrowser=ErrorClient([]))

        status, payload = server.account_check_response({"profile_id": "profile-1", "platform": "bilibili"})

        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "bitbrowser_response_error")


if __name__ == "__main__":
    unittest.main()
