from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import URLError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wt_media_agent.local_api.state import LocalAgentState
from wt_media_agent.local_api.server import LocalApiServer
from wt_media_agent.clients.bitbrowser import BitBrowserClient
from wt_media_agent.clients.bitbrowser.client import _post_json


class ProfileOperationClient:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.opened: list[str] = []
        self.closed: list[str] = []

    def scan_profiles(self):
        raise AssertionError("not used")

    def create_profile(self, config: dict[str, object]) -> str:
        self.created.append(config)
        return "profile-created"

    def open_profile(self, profile_id: str) -> None:
        self.opened.append(profile_id)

    def close_profile(self, profile_id: str) -> None:
        self.closed.append(profile_id)

    def update_profile(self, profile_id: str, config: dict[str, object]) -> None:
        raise AssertionError("not used")

    def delete_profile(self, profile_id: str) -> None:
        raise AssertionError("not used")

    def group_list(self) -> list[dict[str, object]]:
        return [
            {"id": "group-1", "name": "运营分组"},
            {"groupId": "group-2", "groupName": "备用分组"},
            {"name": "missing-id"},
        ]

    def read_cookies(self, profile_id: str) -> list[dict[str, object]]:
        raise AssertionError("not used")


class LenientOpenClient(ProfileOperationClient):
    def open_profile(self, profile_id: str) -> None:
        from wt_media_agent.clients.bitbrowser import BitBrowserResponseError

        raise BitBrowserResponseError("BitBrowser request failed: 浏览器正在打开中")


class LocalProfileOperationTests(unittest.TestCase):
    def test_bitbrowser_not_running_has_actionable_error_for_profile_actions(self) -> None:
        server = LocalApiServer(
            LocalAgentState(),
            bitbrowser=BitBrowserClient("http://127.0.0.1:54345", transport=_post_json, timeout=0.1),
        )
        actions = (
            server.profile_scan_response,
            server.profile_groups_response,
            lambda: server.profile_create_response({"name": "窗口", "groupId": "group-1"}),
            lambda: server.profile_open_response({"id": "profile-1"}),
            lambda: server.profile_close_response({"id": "profile-1"}),
        )

        with patch(
            "wt_media_agent.clients.bitbrowser.client.urlrequest.urlopen",
            side_effect=URLError(ConnectionRefusedError("connection refused")),
        ):
            for action in actions:
                with self.subTest(action=action):
                    status, payload = action()
                    self.assertEqual(status, 503)
                    self.assertEqual(payload["error"]["code"], "bit_browser_offline")
                    self.assertEqual(payload["error"]["recovery_action"], "start_bitbrowser")
                    self.assertNotIn("connection refused", str(payload).lower())

    def test_profile_groups_returns_safe_group_list(self) -> None:
        server = LocalApiServer(LocalAgentState(), bitbrowser=ProfileOperationClient())

        status, payload = server.profile_groups_response()

        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["groups"], [
            {"id": "group-1", "name": "运营分组"},
            {"id": "group-2", "name": "备用分组"},
        ])

    def test_opening_profile_state_is_not_masked_by_route(self) -> None:
        server = LocalApiServer(LocalAgentState(), bitbrowser=LenientOpenClient())

        status, payload = server.profile_open_response({"id": "profile-1"})

        self.assertEqual(status, 502)
        self.assertIn("浏览器正在打开中", payload["error"]["message"])

    def test_profile_create_delegates_to_bitbrowser(self) -> None:
        client = ProfileOperationClient()
        server = LocalApiServer(LocalAgentState(), bitbrowser=client)

        status, payload = server.profile_create_response({"name": "窗口", "groupId": "group-1"})

        self.assertEqual(status, 201)
        self.assertEqual(payload["data"]["id"], "profile-created")
        self.assertEqual(client.created, [{"name": "窗口", "groupId": "group-1"}])

    def test_open_close_delegate_to_bitbrowser(self) -> None:
        client = ProfileOperationClient()
        server = LocalApiServer(LocalAgentState(), bitbrowser=client)

        open_status, open_payload = server.profile_open_response({"id": "profile-1"})
        close_status, close_payload = server.profile_close_response({"id": "profile-1"})

        self.assertEqual(open_status, 200)
        self.assertEqual(open_payload["data"]["status"], "opened")
        self.assertEqual(close_status, 200)
        self.assertEqual(close_payload["data"]["status"], "closed")
        self.assertEqual(client.opened, ["profile-1"])
        self.assertEqual(client.closed, ["profile-1"])


if __name__ == "__main__":
    unittest.main()
