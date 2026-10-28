"""File-transfer loop configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TransferRunnerConfig:
    agent_id: str
    base_url: str
    db_path: str
    poll_interval: float = 5.0
