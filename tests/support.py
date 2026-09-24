"""Shared test doubles. Not a test module -- `test*.py` is the discover pattern.

Kept to things that are only useful *because* they are shared: a double defined
next to a single test tends to inherit that test's assumptions.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from wt_media_agent.runtime.config import reset_config_cache


class UnusedBitBrowser:
    """A BitBrowser stand-in that fails if anything reaches for it.

    `LocalApiServer` needs a client to be constructed (CHG-056 T-04), but most
    routes never touch one. Passing this makes "this route did not consult
    BitBrowser" an assertion rather than an assumption -- a real client would
    have answered, and a `MagicMock` would have swallowed the question.
    """

    def __getattr__(self, name: str):
        raise AssertionError(f"this test must not touch BitBrowser.{name}")


@dataclass(frozen=True)
class IsolatedPaths:
    """A throwaway runtime tree. See `isolated_paths`."""

    root: Path
    data: Path
    logs: Path
    runtime: Path


@contextlib.contextmanager
def isolated_paths() -> Iterator[IsolatedPaths]:
    """A throwaway runtime tree, plus the environment that points the Agent at it.

    CHG-057 D-09 (from the user's ruling) and the architecture baseline §5.13
    require tests to use `tmpdir/data`, `tmpdir/logs`, `tmpdir/runtime` and never
    `REPO_ROOT/.local/data`. This is the only supported way to get there: it sets
    `WT_MEDIA_AGENT_DATA_DIR`, which is the single override the configuration
    layer reads, drops the config cache so the next `get_config()` picks it up,
    and restores both on exit.

    `logs` is `<data>/logs`, not a sibling of `data`. That nesting is the
    override contract `RuntimePaths.resolve` pins (`data_dir` + `logs` +
    `versions`), and re-shaping it here would make the tests agree with
    something production does not do.

    `runtime` has no consumer yet. It exists so a test that needs a scratch
    directory beside the data tree has an obvious place to put it that is not
    the checkout.
    """
    saved = os.environ.get("WT_MEDIA_AGENT_DATA_DIR")

    with tempfile.TemporaryDirectory(prefix="wt-media-agent-test-") as tmp:
        root = Path(tmp)
        paths = IsolatedPaths(
            root=root,
            data=root / "data",
            logs=root / "data" / "logs",
            runtime=root / "runtime",
        )
        for directory in (paths.data, paths.logs, paths.runtime):
            directory.mkdir(parents=True, exist_ok=True)

        os.environ["WT_MEDIA_AGENT_DATA_DIR"] = str(paths.data)
        reset_config_cache()
        try:
            yield paths
        finally:
            if saved is None:
                os.environ.pop("WT_MEDIA_AGENT_DATA_DIR", None)
            else:
                os.environ["WT_MEDIA_AGENT_DATA_DIR"] = saved
            reset_config_cache()
