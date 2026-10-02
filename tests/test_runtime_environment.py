from __future__ import annotations

import unittest

from wt_media_agent.clients.bitbrowser import (
    BitBrowserIdentityError,
    BitBrowserResponseError,
    BitProfile,
    ProfileSnapshot,
)
from wt_media_agent.runtime.environment import (
    BitBrowserScanCache,
    RuntimeEnvironmentCollector,
)


class SnapshotClient:
    def __init__(self) -> None:
        self.calls = 0

    def scan_profiles(self) -> ProfileSnapshot:
        self.calls += 1
        # A different id per call, so a reused answer is distinguishable from a
        # fresh one rather than merely equal by accident.
        return ProfileSnapshot(
            main_user_id=f"main-user-{self.calls}",
            profiles=(
                BitProfile("profile-1", "bit-user-1", "main-user-1", "Account 1", 1, "", "", 1, "", "", "noproxy", "", 0),
                BitProfile("profile-2", "bit-user-2", "main-user-1", "Account 2", 2, "", "", 1, "", "", "noproxy", "", 0),
            ),
        )


class FailingClient:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def scan_profiles(self) -> ProfileSnapshot:
        self.calls += 1
        raise self.error


class SometimesFailingClient(SnapshotClient):
    """Succeeds, then fails: the arm that shows a failure is never cached."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next = False

    def scan_profiles(self) -> ProfileSnapshot:
        if self.fail_next:
            self.calls += 1
            raise BitBrowserResponseError("bitbrowser went away")
        return super().scan_profiles()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _collector(bitbrowser, cache=None) -> RuntimeEnvironmentCollector:
    return RuntimeEnvironmentCollector(
        bitbrowser=bitbrowser,
        scan_cache=cache,
        system_info=lambda: ("Darwin", "arm64"),
        python_version=lambda: "3.12.11",
        command_version=lambda command: "ffmpeg version 7.1",
        workdir_writable=lambda: True,
        disk_free_bytes=lambda: 8 * 1024 * 1024 * 1024,
    )

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


class BitBrowserScanCacheTests(unittest.TestCase):
    """The `scan=reuse` path: what it may reuse, and for how long."""

    def test_a_second_collect_within_the_ttl_does_not_scan_again(self) -> None:
        client = SnapshotClient()
        cache = BitBrowserScanCache(ttl_seconds=300, clock=Clock())

        first = _collector(client, cache).collect().to_dict()
        second = _collector(client, cache).collect().to_dict()

        self.assertEqual(client.calls, 1)
        self.assertEqual(second["main_user_id"], first["main_user_id"])

    def test_a_scan_older_than_the_ttl_is_taken_again(self) -> None:
        client = SnapshotClient()
        clock = Clock()
        cache = BitBrowserScanCache(ttl_seconds=300, clock=clock)

        first = _collector(client, cache).collect().to_dict()
        clock.advance(301)
        second = _collector(client, cache).collect().to_dict()

        self.assertEqual(client.calls, 2)
        self.assertNotEqual(second["main_user_id"], first["main_user_id"])

    def test_the_ttl_boundary_is_a_boundary_and_not_a_rounding(self) -> None:
        """Exactly at the TTL counts as expired: the cheap answer must not drift."""
        client = SnapshotClient()
        clock = Clock()
        cache = BitBrowserScanCache(ttl_seconds=300, clock=clock)

        _collector(client, cache).collect()
        clock.advance(300)
        _collector(client, cache).collect()

        self.assertEqual(client.calls, 2)

    def test_without_a_cache_every_collect_scans(self) -> None:
        """The invariant: a caller that did not ask to reuse never gets a cached scan.

        This is what keeps the bind flow and the runtime report on a live account
        id, and it is the behavior every existing caller has today.
        """
        client = SnapshotClient()

        _collector(client).collect()
        _collector(client).collect()

        self.assertEqual(client.calls, 2)

    def test_a_reused_scan_does_not_paper_over_a_later_failure(self) -> None:
        """Once the cache is stale, a broken BitBrowser reports as broken.

        The point of not caching failures: if the cache were served on expiry too,
        a machine whose BitBrowser just closed would keep reporting the last good
        answer for as long as the process lived.
        """
        client = SometimesFailingClient()
        clock = Clock()
        cache = BitBrowserScanCache(ttl_seconds=300, clock=clock)

        self.assertEqual(_collector(client, cache).collect().to_dict()["bitbrowser_status"], "normal")
        client.fail_next = True
        clock.advance(301)

        self.assertEqual(
            _collector(client, cache).collect().to_dict()["bitbrowser_status"], "unreachable"
        )
        self.assertEqual(client.calls, 2)

    def test_a_recorded_scan_is_what_the_next_reuse_serves(self) -> None:
        """The explicit scan route hands its snapshot over instead of dropping it."""
        client = SnapshotClient()
        cache = BitBrowserScanCache(ttl_seconds=300, clock=Clock())

        recorded = cache.record(client.scan_profiles())
        report = _collector(client, cache).collect().to_dict()

        self.assertEqual(client.calls, 1)
        self.assertEqual(report["main_user_id"], recorded.main_user_id)


if __name__ == "__main__":
    unittest.main()
