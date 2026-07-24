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
    def test_bilibili_cookie_extracts_uid_without_leaking_cookie(self) -> None:
        server = LocalApiServer(bitbrowser=CookieClient([
            {"name": "DedeUserID", "value": "123456"},
            {"name": "SESSDATA", "value": "secret-session"},
        ]))

        status, payload = server.account_check_response({"profile_id": "profile-1", "platform": "bilibili"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["platform_account_id"], "123456")
        self.assertEqual(payload["data"]["login_status"], "normal")
        self.assertNotIn("secret-session", str(payload))

    def test_expected_uid_mismatch_returns_account_mismatch(self) -> None:
        server = LocalApiServer(bitbrowser=CookieClient([{"name": "DedeUserID", "value": "123456"}]))

        status, payload = server.account_check_response({
            "profile_id": "profile-1",
            "platform": "bilibili",
            "expected_platform_account_id": "999",
        })

        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["login_status"], "account_mismatch")

    def test_unknown_platform_identity_is_not_success(self) -> None:
        server = LocalApiServer(bitbrowser=CookieClient([{"name": "BDUSS", "value": "secret"}]))

        status, payload = server.account_check_response({"profile_id": "profile-1", "platform": "baijiahao"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["platform_account_id"], "")
        self.assertEqual(payload["data"]["login_status"], "environment_error")

    def test_bitbrowser_failure_is_mapped_without_cookie_payload(self) -> None:
        server = LocalApiServer(bitbrowser=ErrorClient([]))

        status, payload = server.account_check_response({"profile_id": "profile-1", "platform": "bilibili"})

        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "bitbrowser_response_error")


if __name__ == "__main__":
    unittest.main()
