"""Centralized constants for the WT Media Agent."""

from __future__ import annotations
from enum import StrEnum

# The only remaining claimed work: a download arrives on the transfer surface
# as a `LocalLease`, not through a generic Cloud task route.
TASK_TYPE_MATERIAL_DOWNLOAD = "material_download_task"

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
# profile scan. `GET /api/v1/status?scan=reuse` is the only way to ask; the live
# scan stays the default because the account id it carries is what the bind
# flow acts on.
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

# Storage and Cloud routing. These are separate from the application's build mode.
CLOUD_ENVS = frozenset({'dev', 'test', 'pre', 'prod'})
DEFAULT_CLOUD_ENV = 'dev'
DEFAULT_DB_NAME = 'local-agent.sqlite3'
DEFAULT_DATABASE_SYNCHRONOUS = 'FULL'
DEFAULT_DATABASE_TIMEOUT_SECONDS = 5.0
TASK_TYPE_PUBLICATION = 'publication'
SAVE_DIRECTORY_KEY = 'download.save_directory'

class PublicationStatus(StrEnum):
    """Cloud 定义的发布任务状态；Agent 只消费和报告，不自行裁决。"""

    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting_human"
    UNCERTAIN = "uncertain"
    REVIEWING = "reviewing"
    FAILED = "failed"
    PUBLISHED = "published"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


class PublicationStage(StrEnum):
    """Cloud 执行尝试的阶段，区别于本机提交屏障。"""

    CLAIMED = "claimed"
    PREPARING = "preparing"
    READY = "ready"
    SUBMITTING = "submitting"


class PublicationPhase(StrEnum):
    """本地执行阶段；重启后只用于核实，不授权继续点击。"""

    CLAIMED = "claimed"
    PREPARED = "prepared"
    SUBMITTING = "submitting"
    CLICK_STARTED = "click_started"
    RESULT_SAVED = "result_saved"
    SETTLED = "settled"


class PublicationCheckpointStatus(StrEnum):
    OPEN = "publication_open"
    RESULT = "publication_result"
    SETTLED = "publication_settled"



PUBLICATION_LOCAL_RESULT_STATUSES = frozenset({PublicationStatus.REVIEWING, PublicationStatus.PUBLISHED, PublicationStatus.FAILED,
    PublicationStatus.UNCERTAIN, PublicationStatus.AWAITING_HUMAN, PublicationStatus.CANCELLED})
PUBLICATION_SETTLED_STATUSES = (PublicationStatus.PUBLISHED, PublicationStatus.REVIEWING, PublicationStatus.FAILED,
    PublicationStatus.CANCELLED, PublicationStatus.ABANDONED)

PUBLICATION_RESULT_STATUSES = (PublicationStatus.REVIEWING, PublicationStatus.FAILED, PublicationStatus.UNCERTAIN)
DATABASE_RETRY_INTERVAL_SECONDS = 0.01
DEFAULT_EXECUTION_TIMEOUT_SECONDS = 300
