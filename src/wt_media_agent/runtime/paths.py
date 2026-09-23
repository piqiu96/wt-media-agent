"""Filesystem locations for the Agent's runtime state.

`RuntimePaths` is the single source of truth for where the Agent writes. Three
states, decided in this order:

1. **override** -- a non-empty `data_dir` wins outright, and logs follow it into
   `<data_dir>/logs`. Used by tests, by CI, and by Desktop when it passes
   `WT_MEDIA_AGENT_DATA_DIR` to a bundled sidecar.
2. **dev** -- not frozen and not production: the repository's `.local/`. A
   developer run never touches the installed locations.
3. **installed** -- frozen (PyInstaller) or `environment == "production"`:
   `~/Library/Application Support/WTMedia/Agent` for data and
   `~/Library/Logs/WTMedia/Agent` for logs, per ADR-0016 §5.8.

`data/`, `logs/` and `versions/` are created lazily. Creation failure is never
fatal -- a read-only or sandboxed filesystem must not stop the Agent from
starting and reporting why it cannot write.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

INSTALLED_DATA_DIR = ("Library", "Application Support", "WTMedia", "Agent")
INSTALLED_LOGS_DIR = ("Library", "Logs", "WTMedia", "Agent")

PRODUCTION_ENVIRONMENTS = frozenset({"production"})


def is_frozen() -> bool:
    """Return True when running from a PyInstaller bundle.

    Deliberately reads no environment variable: `runtime/config.py` is the only
    module allowed to do that (CHG-056 T-03). `resolve()` takes `frozen=` so the
    packaged-sidecar branch is testable without building a bundle.
    """
    return bool(getattr(sys, "frozen", False))


def repository_root() -> Path:
    """Return the checkout root, derived from this file's location.

    Only meaningful outside a frozen bundle; `resolve()` consults it in dev
    mode alone.
    """
    return Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class RuntimePaths:
    """Resolved locations, plus which rule produced them."""

    data_dir: Path
    logs_dir: Path
    versions_dir: Path
    origin: str  # "override" | "dev" | "installed"

    @classmethod
    def resolve(
        cls,
        *,
        data_dir: str | None = None,
        environment: str = "development",
        frozen: bool | None = None,
        home: Path | None = None,
        repo_root: Path | None = None,
    ) -> "RuntimePaths":
        """Decide the three directories. Does not touch the filesystem."""
        override = (data_dir or "").strip()
        if override:
            base = Path(override).expanduser()
            return cls(
                data_dir=base,
                logs_dir=base / "logs",
                versions_dir=base / "versions",
                origin="override",
            )

        frozen = is_frozen() if frozen is None else frozen
        installed = frozen or environment.strip().lower() in PRODUCTION_ENVIRONMENTS
        if installed:
            root = (home or Path.home()).expanduser()
            data = root.joinpath(*INSTALLED_DATA_DIR)
            logs = root.joinpath(*INSTALLED_LOGS_DIR)
        else:
            root = repo_root or repository_root()
            data = root / ".local" / "data"
            logs = root / ".local" / "logs"
        return cls(
            data_dir=data,
            logs_dir=logs,
            versions_dir=data / "versions",
            origin="installed" if installed else "dev",
        )

    @property
    def default_log_file(self) -> str:
        """Where logs go when `log_file` is left unset.

        Dev returns "" so the developer reads the terminal. Installed returns a
        real file: a bundled sidecar has no terminal -- Desktop currently
        discards the sidecar's stdout and stderr -- so stderr-only would mean
        the logs go nowhere.
        """
        return "" if self.origin in {"dev", "override"} else str(self.logs_dir / "agent.log")

    def ensure(self) -> tuple[Path, ...]:
        """Create the three directories, skipping any creation that fails.

        Returns the directories newly created. Never raises.
        """
        created: list[Path] = []
        for directory in (self.data_dir, self.logs_dir, self.versions_dir):
            try:
                if not directory.is_dir():
                    directory.mkdir(parents=True, exist_ok=True)
                    created.append(directory)
            except OSError:
                # Read-only or sandboxed filesystem. The Agent still starts and
                # reports the failure through its own error paths.
                continue
        return tuple(created)
