from __future__ import annotations

import unittest

from wt_media_agent.runtimes.bitbrowser import (
    BitBrowserIdentityError,
    BitBrowserResponseError,
    BitProfile,
    ProfileSnapshot,
)
from wt_media_agent.runtimes.environment import RuntimeEnvironmentCollector


class SnapshotClient:
    def scan_profiles(self) -> ProfileSnapshot:
        return ProfileSnapshot(
            main_user_id="main-user-1",
            profiles=(
                BitProfile("profile-1", "bit-user-1", "main-user-1", "Account 1", 1, "", "", 1, "", "", "noproxy", "", 0),
                BitProfile("profile-2", "bit-user-2", "main-user-1", "Account 2", 2, "", "", 1, "", "", "noproxy", "", 0),
            ),
        )


class FailingClient:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def scan_profiles(self) -> ProfileSnapshot:
        raise self.error


class RuntimeEnvironmentCollectorTests(unittest.TestCase):
    def test_collects_normalized_allow_list_and_verified_profiles(self) -> None:
        collector = RuntimeEnvironmentCollector(
            bitbrowser=SnapshotClient(),
            system_info=lambda: ("Darwin", "arm64"),
            python_version=lambda: "3.12.11 (raw build details must be dropped)",
            command_version=lambda command: "ffmpeg version 7.1 Copyright raw details",
            workdir_writable=lambda: True,
            disk_free_bytes=lambda: 8 * 1024 * 1024 * 1024,
            agent_version="0.2.0",
        )

        report = collector.collect().to_dict()

        self.assertEqual(report["operating_system"], "macos")
        self.assertEqual(report["cpu_architecture"], "arm64")
        self.assertEqual(report["agent_version"], "0.2.0")
        self.assertEqual(report["python_version"], "3.12.11")
        self.assertEqual(report["ffmpeg"], {"status": "normal", "version": "7.1"})
        self.assertEqual(report["workdir_status"], "normal")
        self.assertEqual(report["disk"], {"status": "normal", "free_megabytes": 8192})
        self.assertEqual(report["bitbrowser_status"], "normal")
        self.assertEqual(report["main_user_id"], "main-user-1")
        self.assertEqual(report["bit_profile_ids"], ["profile-1", "profile-2"])
        serialized = repr(report).lower()
        for forbidden in ("copyright", "/users/", "cookie", "proxy", "hostname", "127.0.0.1"):
            self.assertNotIn(forbidden, serialized)

    def test_maps_missing_dependency_and_low_disk_without_raw_output(self) -> None:
        collector = RuntimeEnvironmentCollector(
            bitbrowser=FailingClient(BitBrowserResponseError("http://127.0.0.1 secret path")),
            system_info=lambda: ("Windows", "AMD64"),
            python_version=lambda: "3.12.2",
            command_version=lambda command: None,
            workdir_writable=lambda: False,
            disk_free_bytes=lambda: 100 * 1024 * 1024,
        )

        report = collector.collect().to_dict()

        self.assertEqual(report["operating_system"], "windows")
        self.assertEqual(report["cpu_architecture"], "x86_64")
        self.assertEqual(report["ffmpeg"], {"status": "not_installed"})
        self.assertEqual(report["workdir_status"], "user_action_required")
        self.assertEqual(report["disk"]["status"], "abnormal")
        self.assertEqual(report["bitbrowser_status"], "unreachable")
        self.assertNotIn("main_user_id", report)
        self.assertNotIn("bit_profile_ids", report)
        self.assertNotIn("127.0.0.1", repr(report))

    def test_identity_failure_is_distinct_and_reports_no_profile_facts(self) -> None:
        report = RuntimeEnvironmentCollector(
            bitbrowser=FailingClient(BitBrowserIdentityError("mixed secret identities")),
            command_version=lambda command: "ffmpeg version 7.1",
        ).collect().to_dict()

        self.assertEqual(report["bitbrowser_status"], "identity_unverifiable")
        self.assertNotIn("main_user_id", report)
        self.assertNotIn("bit_profile_ids", report)


if __name__ == "__main__":
    unittest.main()
