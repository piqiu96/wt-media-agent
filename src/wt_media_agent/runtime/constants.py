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
TASK_TYPE_PROXY_MUTATION = "proxy_mutation_task"

# The one task type that is not claimed from `/tasks/claim`: a download arrives
# on the transfer surface as a `LocalLease`, so this string is the only thing
# Cloud and this Agent spell the same way about it. Declared here so the
# registry's coverage test sees it and `default_executor_factories` has to
# answer for it.
TASK_TYPE_MATERIAL_DOWNLOAD = "material_download_task"
TASK_TYPE_PROFILE_CREATE = "profile_create_task"
TASK_TYPE_PROFILE_OPEN = "profile_open_task"
TASK_TYPE_PROFILE_CLOSE = "profile_close_task"
TASK_TYPE_PROFILE_UPDATE = "profile_update_task"

# Agent modes.
AGENT_MODE_LOCAL = "local"
AGENT_MODE_CLOUD = "cloud"

# Agent status.
AGENT_STATUS_ONLINE = "online"
AGENT_STATUS_DRAINING = "draining"
AGENT_STATUS_IDLE = "idle"
AGENT_STATUS_RUNNING = "running"

# Default network endpoints.
DEFAULT_CLOUD_BASE_URL = "http://127.0.0.1:8188"
DEFAULT_LOCAL_API_HOST = "127.0.0.1"
DEFAULT_LOCAL_API_PORT = 8765
DEFAULT_BITBROWSER_API_URL = "http://127.0.0.1:54345"
DEFAULT_BITBROWSER_TIMEOUT = 5.0

# How long a caller that explicitly asked to reuse may be served an earlier
# profile scan. `GET /api/v1/status?scan=reuse` is the only way to ask; the
# live scan stays the default because the account id it carries is what Cloud's
# execution gate and the bind flow act on.
BITBROWSER_SCAN_REUSE_SECONDS = 300.0

# Log retention (CHG-057 T-07, rewritten by CHG-058 T-02; the rulings 六 and 三).
# Two bounds remain: a record too large for one line is truncated and marked
# rather than allowed to grow the file, and a rolled file older than 14 days is
# deleted. Neither bounds volume -- how long history stays is bounded, how much of
# it there is is not.
#
# The single-file cap (20 MB) and the three files' shared total (400 MB) are
# gone. The user's 2026-09-24 ruling is explicit that volume is not bounded --
# only how long history is kept ("不需要控制总量，只需要控制能保留多少天超过7天
# 或14天自动删除"). 1 MiB is the answer to CHG-058 Q-01: large enough for any real
# traceback, and the same number the Desktop truncates at, so a line that is
# marked on one side is marked at the same length on the other.
DEFAULT_LOG_MAX_RECORD_BYTES = 1024 * 1024
DEFAULT_LOG_RETENTION_DAYS = 14

# Lease.
DEFAULT_LEASE_SECONDS = 60
MAX_RETRIES = 3
