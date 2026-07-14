"""Agent configuration primitives."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentConfig:
    environment: str = "development"
    cloud_base_url: str = "http://127.0.0.1:8000"
    bitbrowser_api_url: str = "http://127.0.0.1:54345"
    bitbrowser_timeout_seconds: float = 5.0
