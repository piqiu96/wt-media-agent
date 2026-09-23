from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wt_media_agent.clients.bitbrowser import (
    BitBrowserClient,
    BitBrowserIdentityError,
    BitBrowserResponseError,
)
from wt_media_agent.runtime.config import load_config


class FakeTransport:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object], float]] = []

    def __call__(self, url: str, payload: dict[str, object], timeout: float) -> dict[str, object]:
        self.calls.append((url, payload, timeout))
        return self.responses.pop(0)


def profile(index: int, profile_user: str = "bit-user-1", main_user: str = "main-user-1") -> dict[str, object]:
    return {
        "id": f"profile-{index}",
        "name": f"窗口 {index}",
        "seq": index,
        "groupId": "group-1",
        "groupName": "运营组",
        "status": 1,
        "userId": profile_user,
        "mainUserId": main_user,
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

        self.assertEqual(snapshot.main_user_id, "main-user-1")
        self.assertEqual(len(snapshot.profiles), 101)
        self.assertEqual(
            [call[1] for call in transport.calls],
            [{"page": 0, "pageSize": 100}, {"page": 1, "pageSize": 100}],
        )
        self.assertTrue(all(call[0].endswith("/browser/list") for call in transport.calls))
        self.assertTrue(all(call[2] == 3.5 for call in transport.calls))

    def test_maps_main_user_id_as_root_and_profile_user_id_as_profile_owner(self) -> None:
        transport = FakeTransport([{"success": True, "data": {"list": [profile(1)]}}])

        snapshot = BitBrowserClient("http://127.0.0.1:54345", transport=transport).scan_profiles()

        item = snapshot.profiles[0]
        self.assertEqual(snapshot.main_user_id, "main-user-1")
        self.assertEqual(item.main_user_id, "main-user-1")
        self.assertEqual(item.profile_user_id, "bit-user-1")
        self.assertEqual(item.bit_profile_id, "profile-1")
        self.assertNotEqual(item.profile_user_id, "operator-diagnostic")
        encoded = json.dumps(snapshot.to_dict(), ensure_ascii=False)
        for secret in (
            "must-not-leave-adapter",
            "proxy-user-secret",
            "proxy-password-secret",
            "cookie",
            "password",
            "operUserId",
        ):
            self.assertNotIn(secret, encoded)

    def test_reads_proxy_host_and_port_from_official_list_fields(self) -> None:
        transport = FakeTransport([{"success": True, "data": {"list": [{
            **profile(1), "proxyType": "socks5", "host": "198.51.100.10", "port": 50001,
        }]}}])

        snapshot = BitBrowserClient("http://127.0.0.1:54345", transport=transport).scan_profiles()

        self.assertEqual(snapshot.profiles[0].proxy_type, "socks5")
        self.assertEqual(snapshot.profiles[0].proxy_host, "198.51.100.10")
        self.assertEqual(snapshot.profiles[0].proxy_port, 50001)

    def test_accepts_mixed_profile_users_under_one_main_user(self) -> None:
        transport = FakeTransport(
            [{"success": True, "data": {"list": [profile(1), profile(2, profile_user="bit-user-2")]}}]
        )

        snapshot = BitBrowserClient("http://127.0.0.1:54345", transport=transport).scan_profiles()

        self.assertEqual(snapshot.main_user_id, "main-user-1")
        self.assertEqual([profile.profile_user_id for profile in snapshot.profiles], ["bit-user-1", "bit-user-2"])

    def test_rejects_mixed_main_user_identities(self) -> None:
        transport = FakeTransport(
            [{"success": True, "data": {"list": [profile(1), profile(2, main_user="main-user-2")]}}]
        )

        with self.assertRaisesRegex(BitBrowserIdentityError, "mainUserId"):
            BitBrowserClient("http://127.0.0.1:54345", transport=transport).scan_profiles()

    def test_rejects_empty_or_missing_identity(self) -> None:
        for items in (
            [],
            [{**profile(1), "userId": ""}],
            [{key: value for key, value in profile(1).items() if key != "userId"}],
            [{**profile(1), "mainUserId": ""}],
            [{key: value for key, value in profile(1).items() if key != "mainUserId"}],
        ):
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

    def test_create_profile_uses_update_endpoint_and_returns_created_id(self) -> None:
        transport = FakeTransport([{"success": True, "data": {"id": "profile-created"}}])
        client = BitBrowserClient("http://127.0.0.1:54345", transport=transport, timeout=2)

        profile_id = client.create_profile({"name": "窗口", "groupId": "group-1"})

        self.assertEqual(profile_id, "profile-created")
        self.assertEqual(len(transport.calls), 1)
        url, payload, timeout = transport.calls[0]
        self.assertTrue(url.endswith("/browser/update"))
        self.assertEqual(payload, {
            "name": "窗口",
            "groupId": "group-1",
            "browserFingerPrint": {},
            "proxyMethod": 2,
            "proxyType": "noproxy",
        })
        self.assertEqual(timeout, 30.0)

    def test_create_profile_accepts_browser_id_response(self) -> None:
        transport = FakeTransport([{"success": True, "data": {"browserId": "profile-created"}}])

        profile_id = BitBrowserClient("http://127.0.0.1:54345", transport=transport).create_profile({})

        self.assertEqual(profile_id, "profile-created")

    def test_create_profile_keeps_explicit_proxy_config(self) -> None:
        transport = FakeTransport([{"success": True, "data": {"id": "profile-created"}}])

        BitBrowserClient("http://127.0.0.1:54345", transport=transport).create_profile({
            "name": "窗口",
            "host": "127.0.0.1",
            "proxyType": "socks5",
            "port": 1080,
        })

        self.assertEqual(transport.calls[0][1], {
            "name": "窗口",
            "host": "127.0.0.1",
            "proxyType": "socks5",
            "port": 1080,
            "browserFingerPrint": {},
        })

    def test_create_profile_timeout_can_be_overridden(self) -> None:
        transport = FakeTransport([{"success": True, "data": {"id": "profile-created"}}])
        # The variable is now read by the configuration layer, not the client;
        # this still checks the whole path from operator input to request.
        config = load_config(env={"WT_MEDIA_BITBROWSER_CREATE_TIMEOUT_SECONDS": "45"})

        BitBrowserClient(
            "http://127.0.0.1:54345",
            transport=transport,
            timeout=2,
            create_timeout_override=config.bitbrowser_create_timeout_seconds,
        ).create_profile({})

        self.assertEqual(transport.calls[0][2], 45.0)

    def test_profile_mutations_use_long_timeout(self) -> None:
        transport = FakeTransport([
            {"success": True, "data": {}},
            {"success": True, "data": {}},
            {"success": True, "data": {"browserFingerPrint": {"ua": "x"}, "proxyMethod": 2}},
            {"success": True, "data": {}},
        ])
        client = BitBrowserClient("http://127.0.0.1:54345", transport=transport, timeout=2)

        client.open_profile("profile-1")
        client.close_profile("profile-1")
        client.update_profile("profile-1", {"name": "窗口"})

        # open/close 用长超时；update 先读指纹/代理方式（/browser/detail，默认超时）再写回（长超时）
        self.assertEqual([call[2] for call in transport.calls], [30.0, 30.0, 2.0, 30.0])

    def test_profile_mutation_timeout_can_be_overridden(self) -> None:
        transport = FakeTransport([{"success": True, "data": {}}])
        config = load_config(env={"WT_MEDIA_BITBROWSER_MUTATION_TIMEOUT_SECONDS": "60"})

        BitBrowserClient(
            "http://127.0.0.1:54345",
            transport=transport,
            timeout=2,
            mutation_timeout_override=config.bitbrowser_mutation_timeout_seconds,
        ).open_profile("profile-1")

        self.assertEqual(transport.calls[0][2], 60.0)

    def test_update_profile_preserves_current_fingerprint_and_proxy_method(self) -> None:
        transport = FakeTransport([
            {"success": True, "data": {"browserFingerPrint": {"ua": "preserved"}, "proxyMethod": 2}},
            {"success": True, "data": {}},
        ])
        client = BitBrowserClient("http://127.0.0.1:54345", transport=transport, timeout=2)

        client.update_profile("profile-1", {"name": "新名"})

        self.assertEqual(len(transport.calls), 2)
        self.assertTrue(transport.calls[0][0].endswith("/browser/detail"))
        url, payload, _ = transport.calls[1]
        self.assertTrue(url.endswith("/browser/update"))
        self.assertEqual(payload["id"], "profile-1")
        self.assertEqual(payload["name"], "新名")
        self.assertEqual(payload["browserFingerPrint"], {"ua": "preserved"})
        self.assertEqual(payload["proxyMethod"], 2)

    def test_update_profile_keeps_caller_fields_without_detail(self) -> None:
        transport = FakeTransport([{"success": True, "data": {}}])
        client = BitBrowserClient("http://127.0.0.1:54345", transport=transport, timeout=2)

        client.update_profile(
            "profile-1",
            {"name": "窗口", "browserFingerPrint": {"ua": "caller"}, "proxyMethod": 3},
        )

        self.assertEqual(len(transport.calls), 1)
        url, payload, _ = transport.calls[0]
        self.assertTrue(url.endswith("/browser/update"))
        self.assertEqual(payload["browserFingerPrint"], {"ua": "caller"})
        self.assertEqual(payload["proxyMethod"], 3)

    def test_update_profile_raises_when_fingerprint_unreadable(self) -> None:
        transport = FakeTransport([
            {"success": True, "data": {}},
        ])
        client = BitBrowserClient("http://127.0.0.1:54345", transport=transport, timeout=2)

        with self.assertRaises(BitBrowserResponseError):
            client.update_profile("profile-1", {"name": "新名"})

        # 没有对 /browser/update 发请求：宁可拒绝也不传空指纹重置身份
        self.assertEqual(len(transport.calls), 1)

    def test_read_cookies_parses_detail_cookie_field(self) -> None:
        import json as _json
        cookie_payload = _json.dumps([
            {"name": "DedeUserID", "value": "123456", "domain": ".bilibili.com", "path": "/"},
            {"name": "SESSDATA", "value": "secret", "domain": ".bilibili.com", "path": "/"},
        ])
        transport = FakeTransport([{"success": True, "data": {"cookie": cookie_payload}}])
        client = BitBrowserClient("http://127.0.0.1:54345", transport=transport, timeout=2)

        cookies = client.read_cookies("profile-1")

        self.assertEqual(len(cookies), 2)
        self.assertEqual(cookies[0]["name"], "DedeUserID")
        self.assertTrue(transport.calls[0][0].endswith("/browser/detail"))

    def test_read_cookies_returns_empty_when_no_cookie_field(self) -> None:
        transport = FakeTransport([{"success": True, "data": {"name": "窗口"}}])
        client = BitBrowserClient("http://127.0.0.1:54345", transport=transport, timeout=2)

        self.assertEqual(client.read_cookies("profile-1"), [])


if __name__ == "__main__":
    unittest.main()
