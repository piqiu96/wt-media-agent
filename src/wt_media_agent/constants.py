"""Centralized constants for the WT Media Agent."""

from __future__ import annotations

# Task status values (aligned with Cloud task_schemas v2026.07.15.1).
TASK_STATUS_PENDING = "pending"
TASK_STATUS_LEASED = "leased"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCEEDED = "succeeded"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_CANCELLED = "cancelled"

# Task types.
TASK_TYPE_NOOP = "noop_task"
TASK_TYPE_COOKIE_READ = "cookie_read_task"
TASK_TYPE_COOKIE_WRITE = "cookie_write_task"
TASK_TYPE_ACCOUNT_CHECK = "account_check_task"
TASK_TYPE_PROXY_CHECK = "proxy_check_task"

# Agent modes.
AGENT_MODE_LOCAL = "local"
AGENT_MODE_CLOUD = "cloud"

# Agent status.
AGENT_STATUS_ONLINE = "online"
AGENT_STATUS_DRAINING = "draining"
AGENT_STATUS_IDLE = "idle"
AGENT_STATUS_RUNNING = "running"

# Default network endpoints.
DEFAULT_CLOUD_BASE_URL = "http://127.0.0.1:18080"
DEFAULT_LOCAL_API_HOST = "127.0.0.1"
DEFAULT_LOCAL_API_PORT = 8765
DEFAULT_BITBROWSER_API_URL = "http://127.0.0.1:54345"
DEFAULT_BITBROWSER_TIMEOUT = 5.0

# Lease.
DEFAULT_LEASE_SECONDS = 60
MAX_RETRIES = 3
