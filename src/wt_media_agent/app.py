"""Shared Agent application shell."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentApp:
    """Minimal app shell for Local Agent and Cloud Agent modes."""

    mode: str

    def run(self) -> int:
        print(f"wt-media-agent scaffold ready: mode={self.mode}")
        return 0


def create_app(mode: str) -> AgentApp:
    return AgentApp(mode=mode)
