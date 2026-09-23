"""Double-lock guard for future sensitive Browser Profile executors."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Iterator, Mapping, Protocol


class ProfileBusyError(RuntimeError):
    """The Profile already has a local sensitive operation."""


class SensitiveTaskWaiting(RuntimeError):
    """Cloud reports an active conflicting Profile permit."""


class SensitiveTaskReviewRequired(RuntimeError):
    """An earlier permit has an uncertain external result."""


class GuardClient(Protocol):
    def preflight_sensitive_task(
        self, node_id: str, credential: str, task_id: str
    ) -> Mapping[str, object]: ...

    def finish_sensitive_permit(
        self,
        node_id: str,
        node_credential: str,
        permit_id: str,
        permit_credential: str,
        outcome: str,
    ) -> Mapping[str, object]: ...


@dataclass
class ProfileLease:
    manager: "ProfileLockManager"
    profile_id: str
    task_id: str
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self.manager._release(self.profile_id, self.task_id)
        self._released = True


class ProfileLockManager:
    """Process-local exclusive ownership keyed by Cloud Profile ID."""

    def __init__(self) -> None:
        self._mutex = Lock()
        self._owners: dict[str, str] = {}

    def acquire(self, profile_id: str, task_id: str) -> ProfileLease:
        if not profile_id.strip() or not task_id.strip():
            raise ValueError("profile_id and task_id are required")
        with self._mutex:
            if profile_id in self._owners:
                raise ProfileBusyError("Profile already has a local sensitive task")
            self._owners[profile_id] = task_id
        return ProfileLease(self, profile_id, task_id)

    def is_locked(self, profile_id: str) -> bool:
        with self._mutex:
            return profile_id in self._owners

    def _release(self, profile_id: str, task_id: str) -> None:
        with self._mutex:
            if self._owners.get(profile_id) == task_id:
                del self._owners[profile_id]


class SensitiveTaskGuard:
    """Acquires local lock first, then a credentialed Cloud permit."""

    def __init__(
        self,
        client: GuardClient,
        locks: ProfileLockManager,
        *,
        node_id: str,
        node_credential: str,
    ) -> None:
        self._client = client
        self._locks = locks
        self._node_id = node_id
        self._node_credential = node_credential

    @contextmanager
    def acquire(self, task_id: str, profile_id: str) -> Iterator[Mapping[str, object]]:
        local = self._locks.acquire(profile_id, task_id)
        permit: Mapping[str, object] | None = None
        try:
            permit = self._client.preflight_sensitive_task(
                self._node_id, self._node_credential, task_id
            )
            outcome = permit.get("outcome")
            if outcome == "waiting":
                raise SensitiveTaskWaiting("Profile is held by another sensitive task")
            if outcome == "review_required":
                raise SensitiveTaskReviewRequired("prior sensitive task result needs review")
            if outcome != "granted":
                raise RuntimeError("Cloud sensitive-task preflight was rejected")
            if permit.get("profile_id") != profile_id:
                raise RuntimeError("Cloud permit Profile does not match the local lock")
            yield permit
        except (SensitiveTaskWaiting, SensitiveTaskReviewRequired):
            raise
        except BaseException:
            if permit is not None and permit.get("outcome") == "granted":
                self._finish(permit, "result_uncertain")
            raise
        else:
            if permit is not None:
                self._finish(permit, "completed")
        finally:
            local.release()

    def _finish(self, permit: Mapping[str, object], outcome: str) -> None:
        permit_id = permit.get("permit_id")
        permit_credential = permit.get("permit_credential")
        if not isinstance(permit_id, str) or not isinstance(permit_credential, str):
            raise RuntimeError("Cloud permit omitted its credential")
        self._client.finish_sensitive_permit(
            self._node_id,
            self._node_credential,
            permit_id,
            permit_credential,
            outcome,
        )
