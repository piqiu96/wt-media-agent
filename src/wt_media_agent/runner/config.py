"""TaskRunner configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TaskRunnerConfig:
    agent_id: str
    base_url: str
    db_path: str
    poll_interval: float = 5.0
    lease_seconds: int = 60
    max_retries: int = 3
