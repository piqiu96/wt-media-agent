from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wt_media_agent.runtimes.bitbrowser import (
    BitBrowserClient,
    BitBrowserIdentityError,
    BitBrowserResponseError,
)


class FakeTransport:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object], float]] = []

    def __call__(self, url: str, payload: dict[str, object], timeout: float) -> dict[str, object]:
        self.calls.append((url, payload, timeout))
        return self.responses.pop(0)


def profile(index: int, owner: str = "bit-user-1") -> dict[str, object]:
    return {
        "id": f"profile-{index}",
        "name": f"窗口 {index}",
        "seq": index,
        "groupId": "group-1",
        "groupName": "运营组",
        "status": 1,
        "userId": owner,
        "mainUserId": "main-user-diagnostic",
        "operUserId": "operator-diagnostic",
        "updateTime": "2026-07-14 18:00:00",
        "cookie": "must-not-leave-adapter",
        "password": "must-not-leave-adapter",
        "proxyUserName": "proxy-user-secret",
        "proxyPassword": "proxy-password-secret",
    }


class BitBrowserRuntimeTests(unittest.TestCase):
    def test_pages_from_zero_with_official_max_page_size(self) -> None:
        first_page = [profile(index) for index in range(100)]
        second_page = [profile(100)]
        transport = FakeTransport(
            [
                {"success": True, "data": {"list": first_page}},
                {"success": True, "data": {"list": second_page}},
            ]
        )
        client = BitBrowserClient("http://127.0.0.1:54345", transport=transport, timeout=3.5)

        snapshot = client.scan_profiles()

        self.assertEqual(snapshot.owner_user_id, "bit-user-1")
        self.assertEqual(len(snapshot.profiles), 101)
        self.assertEqual(
            [call[1] for call in transport.calls],
            [{"page": 0, "pageSize": 100}, {"page": 1, "pageSize": 100}],
        )
        self.assertTrue(all(call[0].endswith("/browser/list") for call in transport.calls))
        self.assertTrue(all(call[2] == 3.5 for call in transport.calls))

    def test_maps_only_user_id_to_owner_and_strips_secrets(self) -> None:
        transport = FakeTransport([{"success": True, "data": {"list": [profile(1)]}}])

        snapshot = BitBrowserClient("http://127.0.0.1:54345", transport=transport).scan_profiles()

        item = snapshot.profiles[0]
        self.assertEqual(item.owner_user_id, "bit-user-1")
        self.assertEqual(item.bit_profile_id, "profile-1")
        self.assertNotEqual(item.owner_user_id, "operator-diagnostic")
        encoded = json.dumps(snapshot.to_dict(), ensure_ascii=False)
        for secret in (
            "must-not-leave-adapter",
            "proxy-user-secret",
            "proxy-password-secret",
            "cookie",
            "password",
            "operUserId",
            "mainUserId",
        ):
            self.assertNotIn(secret, encoded)

    def test_rejects_mixed_profile_identities(self) -> None:
        transport = FakeTransport(
            [{"success": True, "data": {"list": [profile(1), profile(2, owner="bit-user-2")]}}]
        )

        with self.assertRaisesRegex(BitBrowserIdentityError, "mixed"):
            BitBrowserClient("http://127.0.0.1:54345", transport=transport).scan_profiles()

    def test_rejects_empty_or_missing_identity(self) -> None:
        for items in ([], [{**profile(1), "userId": ""}], [{key: value for key, value in profile(1).items() if key != "userId"}]):
            with self.subTest(items=items):
                transport = FakeTransport([{"success": True, "data": {"list": items}}])
                with self.assertRaises(BitBrowserIdentityError):
                    BitBrowserClient("http://127.0.0.1:54345", transport=transport).scan_profiles()

    def test_rejects_failed_or_malformed_responses(self) -> None:
        responses = (
            {"success": False, "msg": "not logged in"},
            {"success": True, "data": {}},
            {"success": True, "data": {"list": "not-a-list"}},
        )
        for response in responses:
            with self.subTest(response=response):
                transport = FakeTransport([response])
                with self.assertRaises(BitBrowserResponseError):
                    BitBrowserClient("http://127.0.0.1:54345", transport=transport).scan_profiles()


if __name__ == "__main__":
    unittest.main()
