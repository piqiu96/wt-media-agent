"""Local Agent observable state and pending result queue."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Mapping, Optional


@dataclass
class PendingResultQueue:
    _items: Deque[dict[str, object]] = field(default_factory=deque)

    def add(self, result: Mapping[str, object]) -> None:
        self._items.append(dict(result))

    def drain(self) -> list[dict[str, object]]:
        drained = list(self._items)
        self._items.clear()
        return drained

    def size(self) -> int:
        return len(self._items)


@dataclass
class LocalAgentState:
    node_id: str = ""
    agent_id: str = "local-agent-dev"
    status: str = "idle"
    current_task_id: Optional[str] = None
    pending_results: PendingResultQueue = field(default_factory=PendingResultQueue)

    def snapshot(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "agent_id": self.agent_id,
            "status": self.status,
            "current_task_id": self.current_task_id,
            "pending_result_count": self.pending_results.size(),
        }

    def sse_snapshot(self) -> str:
        return "event: status\ndata: " + _json_like(self.snapshot()) + "\n\n"


def _json_like(value: Mapping[str, object]) -> str:
    import json

    return json.dumps(value, separators=(",", ":"))
