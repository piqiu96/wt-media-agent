"""Executor protocol types.

Declared in `executors/` rather than `runner/` so that executors and the
registry can type themselves against these names without importing `runner/`:
ADR-0016 orders the descent `runner -> executors`, so `executors -> runner`
would point the wrong way.

These are type-only declarations. `TaskRunner` still detects a usable executor
with `hasattr(instance, "execute")`, exactly as before -- no runtime check was
introduced here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Callable, Protocol

from wt_media_agent.clients.cloud import CloudAgentClient


class Executor(Protocol):
    """A single task-type executor."""

    def execute(self, task: Mapping[str, object]) -> Mapping[str, object]: ...


ExecutorFactory = Callable[[CloudAgentClient, str], Executor]
