"""Agent configuration loaded from environment with sensible defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from wt_media_agent.constants import (
    DEFAULT_BITBROWSER_API_URL,
    DEFAULT_BITBROWSER_TIMEOUT,
    DEFAULT_CLOUD_BASE_URL,
    DEFAULT_LOCAL_API_HOST,
    DEFAULT_LOCAL_API_PORT,
)


@dataclass(frozen=True)
class AgentConfig:
    # Environment.
    environment: str = field(default_factory=lambda: os.getenv("WT_MEDIA_ENV", "development"))

    # Cloud API.
    cloud_base_url: str = field(default_factory=lambda: os.getenv("WT_MEDIA_CLOUD_BASE_URL", DEFAULT_CLOUD_BASE_URL))
    cloud_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("WT_MEDIA_CLOUD_TIMEOUT", "10")))

    # Local API.
    local_api_host: str = field(default_factory=lambda: os.getenv("WT_MEDIA_LOCAL_API_HOST", DEFAULT_LOCAL_API_HOST))
    local_api_port: int = field(default_factory=lambda: int(os.getenv("WT_MEDIA_LOCAL_API_PORT", str(DEFAULT_LOCAL_API_PORT))))

    # BitBrowser.
    bitbrowser_api_url: str = field(default_factory=lambda: os.getenv("WT_MEDIA_BITBROWSER_API_URL", DEFAULT_BITBROWSER_API_URL))
    bitbrowser_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("WT_MEDIA_BITBROWSER_TIMEOUT_SECONDS", str(DEFAULT_BITBROWSER_TIMEOUT))))

    # Data directory.
    data_dir: str = field(default_factory=lambda: os.getenv("WT_MEDIA_AGENT_DATA_DIR", str(Path.home() / ".wt-media-agent")))

    # Logging.
    log_level: str = field(default_factory=lambda: os.getenv("WT_MEDIA_LOG_LEVEL", "INFO"))
    log_file: str = field(default_factory=lambda: os.getenv("WT_MEDIA_LOG_FILE", ""))

    @property
    def local_api_address(self) -> str:
        return f"{self.local_api_host}:{self.local_api_port}"
