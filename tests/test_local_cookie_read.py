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


class LocalCookieReadTests(unittest.TestCase):
    def test_cookie_read_returns_real_cookies(self) -> None:
        server = LocalApiServer(bitbrowser=CookieClient([
            {"name": "DedeUserID", "value": "123456"},
            {"name": "SESSDATA", "value": "session"},
        ]))

        status, payload = server.cookie_read_response({"profile_id": "profile-1"})

        self.assertEqual(status, 200)
        self.assertEqual(len(payload["data"]["cookies"]), 2)
        self.assertTrue(server.bitbrowser.opened == ["profile-1"])

    def test_cookie_read_requires_profile_id(self) -> None:
        server = LocalApiServer(bitbrowser=CookieClient([]))

        status, payload = server.cookie_read_response({})

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "cookie_read_input_invalid")

    def test_cookie_read_maps_bitbrowser_failure(self) -> None:
        server = LocalApiServer(bitbrowser=ErrorClient([]))

        status, payload = server.cookie_read_response({"profile_id": "profile-1"})

        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "bitbrowser_response_error")


if __name__ == "__main__":
    unittest.main()
