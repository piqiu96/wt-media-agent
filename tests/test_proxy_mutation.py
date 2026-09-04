from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wt_media_agent.local_api.server import LocalApiServer
from wt_media_agent.runtimes.bitbrowser import BitProfile, ProfileSnapshot


class MutationClient:
    def __init__(self, apply_updates: bool = False) -> None:
        self.updated: list[tuple[str, dict[str, object]]] = []
        self.apply_updates = apply_updates
        self.proxy_type = "socks5"
        self.proxy_host = "127.0.0.1"
        self.proxy_port = 1080

    def update_profile(self, profile_id: str, config: dict[str, object]) -> None:
        self.updated.append((profile_id, config))
        if not self.apply_updates:
            return
        self.proxy_type = str(config.get("proxyType", ""))
        self.proxy_host = str(config.get("proxyHost", ""))
        self.proxy_port = int(config.get("proxyPort", 0) or 0)

    def scan_profiles(self) -> ProfileSnapshot:
        return ProfileSnapshot("main-1", (BitProfile(
            bit_profile_id="bit-profile-1", profile_user_id="user-1", main_user_id="main-1",
            name="窗口", seq=1, group_id="group-1", group_name="分组", status=1,
            bit_updated_at="2026-09-04T00:00:00Z", remark="", proxy_type=self.proxy_type,
            proxy_host=self.proxy_host, proxy_port=self.proxy_port,
        ),))


class ProxyMutationTests(unittest.TestCase):
    def test_mutation_writes_then_returns_secret_free_readback(self) -> None:
        client = MutationClient()
        status, payload = LocalApiServer(bitbrowser=client).proxy_mutation_response({
            "profile_id": "bit-profile-1", "proxy_protocol": "socks5", "host": "127.0.0.1", "port": 1080,
            "username": "secret-user", "password": "secret-password",
        })

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"data": {"profile_id": "bit-profile-1", "proxy_protocol": "socks5", "host": "127.0.0.1", "port": 1080, "readback": True}})
        self.assertEqual(client.updated, [("bit-profile-1", {"proxyType": "socks5", "proxyHost": "127.0.0.1", "proxyPort": 1080, "proxyUserName": "secret-user", "proxyPassword": "secret-password"})])
        self.assertNotIn("password", str(payload).lower())

    def test_mutation_does_not_report_success_when_readback_differs(self) -> None:
        client = MutationClient()
        status, payload = LocalApiServer(bitbrowser=client).proxy_mutation_response({
            "profile_id": "bit-profile-1", "proxy_protocol": "http", "host": "127.0.0.1", "port": 1080,
        })

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "proxy_mutation_readback_mismatch")

    def test_unbind_writes_no_proxy_and_requires_no_proxy_readback(self) -> None:
        client = MutationClient(apply_updates=True)

        status, payload = LocalApiServer(bitbrowser=client).proxy_mutation_response({
            "operation": "unbind", "profile_id": "bit-profile-1",
        })

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"data": {"operation": "unbind", "profile_id": "bit-profile-1", "readback": True}})
        self.assertEqual(client.updated, [("bit-profile-1", {"proxyType": "noproxy"})])


if __name__ == "__main__":
    unittest.main()
