from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wt_media_agent.local_api.state import LocalAgentState
from wt_media_agent.local_api.server import LocalApiServer
from wt_media_agent.clients.bitbrowser import (
    BitBrowserIdentityError,
    BitBrowserResponseError,
    BitProfile,
    ProfileSnapshot,
)


class SnapshotClient:
    def scan_profiles(self) -> ProfileSnapshot:
        return ProfileSnapshot(
            main_user_id="main-user-1",
            profiles=(
                BitProfile(
                    bit_profile_id="profile-1",
                    profile_user_id="bit-user-1",
                    main_user_id="main-user-1",
                    name="运营窗口",
                    seq=1,
                    group_id="group-1",
                    group_name="运营组",
                    status=1,
                    bit_updated_at="2026-07-14 18:00:00",
                    remark="",
                    proxy_type="noproxy",
                    proxy_host="",
                    proxy_port=0,
                ),
            ),
        )


class ErrorClient:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def scan_profiles(self) -> ProfileSnapshot:
        raise self.error


class LocalProfileScanTests(unittest.TestCase):
    def test_returns_secret_free_scan_payload(self) -> None:
        status, payload = LocalApiServer(LocalAgentState(), bitbrowser=SnapshotClient()).profile_scan_response()

        self.assertEqual(status, 200)
        self.assertEqual(payload["main_user_id"], "main-user-1")
        self.assertEqual(payload["profiles"][0]["bit_profile_id"], "profile-1")
        self.assertEqual(payload["profiles"][0]["profile_user_id"], "bit-user-1")
        self.assertEqual(payload["profiles"][0]["main_user_id"], "main-user-1")
        self.assertEqual(payload["profiles"][0]["status"], 1)
        self.assertEqual(payload["profiles"][0]["proxy_type"], "noproxy")
        self.assertEqual(payload["profiles"][0]["proxy_port"], 0)
        self.assertNotIn("cookie", str(payload).lower())
        self.assertNotIn("password", str(payload).lower())

    def test_maps_identity_failure_without_raw_response(self) -> None:
        status, payload = LocalApiServer(
            LocalAgentState(),
            bitbrowser=ErrorClient(BitBrowserIdentityError("mixed secret diagnostic"))
        ).profile_scan_response()

        self.assertEqual(status, 409)
        self.assertEqual(payload, {"error": {"code": "bitbrowser_identity_unverifiable"}})

    def test_maps_local_api_failure(self) -> None:
        status, payload = LocalApiServer(
            LocalAgentState(),
            bitbrowser=ErrorClient(BitBrowserResponseError("raw upstream failure"))
        ).profile_scan_response()

        self.assertEqual(status, 502)
        self.assertEqual(payload, {"error": {"code": "bitbrowser_response_error"}})


if __name__ == "__main__":
    unittest.main()
