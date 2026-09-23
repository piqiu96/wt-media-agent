from __future__ import annotations

import unittest

from wt_media_agent.services.profile_guard import (
    ProfileBusyError,
    ProfileLockManager,
    SensitiveTaskGuard,
    SensitiveTaskWaiting,
)


class FakeCloudGuardClient:
    def __init__(self, locks: ProfileLockManager, *, outcome: str = "granted") -> None:
        self.locks = locks
        self.outcome = outcome
        self.calls: list[tuple[object, ...]] = []

    def preflight_sensitive_task(self, node_id: str, credential: str, task_id: str) -> dict[str, object]:
        self.calls.append(("preflight", node_id, task_id, self.locks.is_locked("profile-1")))
        if self.outcome != "granted":
            return {"outcome": self.outcome}
        return {
            "outcome": "granted",
            "permit_id": "permit-1",
            "permit_credential": "permit-secret",
            "profile_id": "profile-1",
        }

    def finish_sensitive_permit(
        self,
        node_id: str,
        node_credential: str,
        permit_id: str,
        permit_credential: str,
        outcome: str,
    ) -> dict[str, object]:
        self.calls.append(("finish", permit_id, outcome))
        return {"status": outcome}


class ProfileGuardTests(unittest.TestCase):
    def test_local_profile_lock_is_exclusive_and_releases(self) -> None:
        locks = ProfileLockManager()
        first = locks.acquire("profile-1", "task-1")
        self.assertTrue(locks.is_locked("profile-1"))

        with self.assertRaises(ProfileBusyError):
            locks.acquire("profile-1", "task-2")

        first.release()
        second = locks.acquire("profile-1", "task-2")
        self.assertEqual(second.task_id, "task-2")
        second.release()
        self.assertFalse(locks.is_locked("profile-1"))

    def test_guard_locks_locally_before_cloud_preflight_and_releases_cleanly(self) -> None:
        locks = ProfileLockManager()
        cloud = FakeCloudGuardClient(locks)
        guard = SensitiveTaskGuard(cloud, locks, node_id="node-1", node_credential="node-secret")

        with guard.acquire("task-1", "profile-1") as permit:
            self.assertEqual(permit["permit_id"], "permit-1")
            self.assertTrue(locks.is_locked("profile-1"))

        self.assertEqual(cloud.calls[0], ("preflight", "node-1", "task-1", True))
        self.assertEqual(cloud.calls[1], ("finish", "permit-1", "completed"))
        self.assertFalse(locks.is_locked("profile-1"))

    def test_waiting_preflight_releases_local_lock(self) -> None:
        locks = ProfileLockManager()
        guard = SensitiveTaskGuard(
            FakeCloudGuardClient(locks, outcome="waiting"),
            locks,
            node_id="node-1",
            node_credential="node-secret",
        )

        with self.assertRaises(SensitiveTaskWaiting):
            with guard.acquire("task-1", "profile-1"):
                self.fail("waiting task must not execute")

        self.assertFalse(locks.is_locked("profile-1"))

    def test_execution_exception_marks_permit_uncertain(self) -> None:
        locks = ProfileLockManager()
        cloud = FakeCloudGuardClient(locks)
        guard = SensitiveTaskGuard(cloud, locks, node_id="node-1", node_credential="node-secret")

        with self.assertRaisesRegex(RuntimeError, "external result unknown"):
            with guard.acquire("task-1", "profile-1"):
                raise RuntimeError("external result unknown")

        self.assertEqual(cloud.calls[1], ("finish", "permit-1", "result_uncertain"))
        self.assertFalse(locks.is_locked("profile-1"))


if __name__ == "__main__":
    unittest.main()
