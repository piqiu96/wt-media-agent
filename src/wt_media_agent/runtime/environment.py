"""Allow-listed local runtime environment facts for Cloud attestation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import time
from typing import Callable, Protocol

from wt_media_agent.runtime.version import __version__
from wt_media_agent.clients.bitbrowser import (
    BitBrowserError,
    BitBrowserIdentityError,
    ProfileSnapshot,
)


class ProfileScanner(Protocol):
    def scan_profiles(self) -> ProfileSnapshot: ...


@dataclass(frozen=True)
class DependencyFact:
    status: str
    version: str = ""

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {"status": self.status}
        if self.version:
            value["version"] = self.version
        return value


@dataclass(frozen=True)
class DiskFact:
    status: str
    free_megabytes: int


@dataclass(frozen=True)
class RuntimeEnvironmentReport:
    operating_system: str
    cpu_architecture: str
    agent_version: str
    python_version: str
    ffmpeg: DependencyFact
    workdir_status: str
    disk: DiskFact
    bitbrowser_status: str
    main_user_id: str = ""
    bit_profile_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "operating_system": self.operating_system,
            "cpu_architecture": self.cpu_architecture,
            "agent_version": self.agent_version,
            "python_version": self.python_version,
            "ffmpeg": self.ffmpeg.to_dict(),
            "workdir_status": self.workdir_status,
            "disk": asdict(self.disk),
            "bitbrowser_status": self.bitbrowser_status,
        }
        if self.main_user_id:
            value["main_user_id"] = self.main_user_id
            value["bit_profile_ids"] = list(self.bit_profile_ids)
        return value


SystemInfo = Callable[[], tuple[str, str]]
VersionProbe = Callable[[tuple[str, ...]], str | None]


class BitBrowserScanCache:
    """Remembers the last successful profile scan for a bounded while.

    The scan is the expensive half of `/api/v1/status`: one `POST
    /browser/list` per 100 profiles against the BitBrowser app, measured at
    0.22-0.31 s and 78 KB on a 40-profile machine, asked for every 30 s by the
    top bar's status pill. A caller that only wants a verdict may reuse a scan
    up to `ttl_seconds` old instead of paying that again.

    Only *successful* scans are kept. A failed one raises before anything is
    recorded, so a closed BitBrowser reports `unreachable` rather than being
    papered over with the last good answer.

    Reuse can therefore describe a machine that has since changed -- that is
    what it is for, and it is why the live scan stays the default: the account
    id this carries is what Cloud's execution gate and the bind flow act on,
    and a stale id there is a wrong answer rather than a late one.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._snapshot: ProfileSnapshot | None = None
        self._taken_at = 0.0

    def snapshot(self, scanner: ProfileScanner) -> ProfileSnapshot:
        now = self._clock()
        if self._snapshot is not None and now - self._taken_at < self._ttl_seconds:
            return self._snapshot
        return self.record(scanner.scan_profiles())

    def record(self, snapshot: ProfileSnapshot) -> ProfileSnapshot:
        """Adopt a snapshot someone else just took, so the next reuse sees it."""
        self._snapshot = snapshot
        self._taken_at = self._clock()
        return snapshot


class RuntimeEnvironmentCollector:
    """Collects only facts explicitly approved for a runtime heartbeat."""

    MINIMUM_FREE_BYTES = 1024 * 1024 * 1024

    def __init__(
        self,
        *,
        bitbrowser: ProfileScanner,
        scan_cache: BitBrowserScanCache | None = None,
        system_info: SystemInfo | None = None,
        python_version: Callable[[], str] | None = None,
        command_version: VersionProbe | None = None,
        workdir_writable: Callable[[], bool] | None = None,
        disk_free_bytes: Callable[[], int] | None = None,
        agent_version: str = __version__,
    ) -> None:
        self._bitbrowser = bitbrowser
        self._scan_cache = scan_cache
        self._system_info = system_info or (lambda: (platform.system(), platform.machine()))
        self._python_version = python_version or (lambda: platform.python_version())
        self._command_version = command_version or _command_version
        self._workdir_writable = workdir_writable or (lambda: os.access(Path.cwd(), os.W_OK))
        self._disk_free_bytes = disk_free_bytes or (lambda: shutil.disk_usage(Path.cwd()).free)
        self._agent_version = agent_version

    def collect(self) -> RuntimeEnvironmentReport:
        system, machine = self._system_info()
        free_bytes = max(0, int(self._disk_free_bytes()))
        ffmpeg_output = self._command_version(("ffmpeg", "-version"))
        ffmpeg = DependencyFact(
            status="normal" if ffmpeg_output else "not_installed",
            version=_extract_version(ffmpeg_output or ""),
        )
        bitbrowser_status = "normal"
        main_user_id = ""
        profile_ids: tuple[str, ...] = ()
        try:
            snapshot = (
                self._scan_cache.snapshot(self._bitbrowser)
                if self._scan_cache is not None
                else self._bitbrowser.scan_profiles()
            )
            main_user_id = snapshot.main_user_id
            profile_ids = tuple(sorted(profile.bit_profile_id for profile in snapshot.profiles))
        except BitBrowserIdentityError:
            bitbrowser_status = "identity_unverifiable"
        except BitBrowserError:
            bitbrowser_status = "unreachable"

        return RuntimeEnvironmentReport(
            operating_system=_normalize_system(system),
            cpu_architecture=_normalize_architecture(machine),
            agent_version=_version_only(self._agent_version),
            python_version=_version_only(self._python_version()),
            ffmpeg=ffmpeg,
            workdir_status="normal" if self._workdir_writable() else "user_action_required",
            disk=DiskFact(
                status="normal" if free_bytes >= self.MINIMUM_FREE_BYTES else "abnormal",
                free_megabytes=free_bytes // (1024 * 1024),
            ),
            bitbrowser_status=bitbrowser_status,
            main_user_id=main_user_id,
            bit_profile_ids=profile_ids,
        )


def _normalize_system(value: str) -> str:
    normalized = value.strip().lower()
    return {"darwin": "macos", "windows": "windows", "linux": "linux"}.get(
        normalized, "unsupported"
    )


def _normalize_architecture(value: str) -> str:
    normalized = value.strip().lower()
    return {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(normalized, "unsupported")


def _extract_version(value: str) -> str:
    match = re.search(r"\bversion\s+([0-9]+(?:\.[0-9A-Za-z-]+)*)", value, re.IGNORECASE)
    return match.group(1) if match else ""


def _version_only(value: str) -> str:
    match = re.search(r"\d+(?:\.\d+){1,3}", value)
    return match.group(0) if match else "unknown"


def _command_version(command: tuple[str, ...]) -> str | None:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout[:256]
