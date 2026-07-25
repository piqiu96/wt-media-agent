from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wt_media_agent.local_api.server import LocalApiServer


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
        from wt_media_agent.runtimes.bitbrowser import BitBrowserResponseError

        raise BitBrowserResponseError("BitBrowser request failed: 浏览器正在打开中")


class LocalProfileOperationTests(unittest.TestCase):
    def test_profile_groups_returns_safe_group_list(self) -> None:
        server = LocalApiServer(bitbrowser=ProfileOperationClient())

        status, payload = server.profile_groups_response()

        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["groups"], [
            {"id": "group-1", "name": "运营分组"},
            {"id": "group-2", "name": "备用分组"},
        ])

    def test_opening_profile_state_is_not_masked_by_route(self) -> None:
        server = LocalApiServer(bitbrowser=LenientOpenClient())

        status, payload = server.profile_open_response({"id": "profile-1"})

        self.assertEqual(status, 502)
        self.assertIn("浏览器正在打开中", payload["error"]["message"])

    def test_profile_create_delegates_to_bitbrowser(self) -> None:
        client = ProfileOperationClient()
        server = LocalApiServer(bitbrowser=client)

        status, payload = server.profile_create_response({"name": "窗口", "groupId": "group-1"})

        self.assertEqual(status, 201)
        self.assertEqual(payload["data"]["id"], "profile-created")
        self.assertEqual(client.created, [{"name": "窗口", "groupId": "group-1"}])

    def test_open_close_delegate_to_bitbrowser(self) -> None:
        client = ProfileOperationClient()
        server = LocalApiServer(bitbrowser=client)

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
