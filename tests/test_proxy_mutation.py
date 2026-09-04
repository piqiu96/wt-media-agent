from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wt_media_agent.local_api.server import LocalApiServer
from wt_media_agent.runtimes.bitbrowser import BitProfile, ProfileSnapshot


class MutationClient:
    def __init__(self) -> None:
        self.updated: list[tuple[str, dict[str, object]]] = []

    def update_profile(self, profile_id: str, config: dict[str, object]) -> None:
        self.updated.append((profile_id, config))

    def scan_profiles(self) -> ProfileSnapshot:
        return ProfileSnapshot("main-1", (BitProfile(
            bit_profile_id="bit-profile-1", profile_user_id="user-1", main_user_id="main-1",
            name="窗口", seq=1, group_id="group-1", group_name="分组", status=1,
            bit_updated_at="2026-09-04T00:00:00Z", remark="", proxy_type="socks5",
            proxy_host="127.0.0.1", proxy_port=1080,
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


if __name__ == "__main__":
    unittest.main()
