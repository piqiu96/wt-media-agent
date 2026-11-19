from dataclasses import dataclass
from typing import Optional

@dataclass
class TaskCheckpoint:
    task_id: str
    task_type: str
    agent_id: str
    checkpoint_status: str  # claimed, running, completed, failed
    progress: int = 0
    message: Optional[str] = None
    result_json: Optional[str] = None
    error_code: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    retry_count: int = 0
    #: A `TransferResume` in its encoded form (migration `0003_transfer_resume`).
    transfer_state_json: Optional[str] = None
    attempt_id: Optional[str] = None
    task_payload_json: Optional[str] = None
    last_stage: Optional[str] = None
    checkpoint_json: Optional[str] = None
    final_status: Optional[str] = None
    final_result_json: Optional[str] = None
    error_message: Optional[str] = None
    report_pending: int = 0
    last_report_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    execution_timeout_seconds: int = 300
    external_side_effect: int = 0

@dataclass
class OfflineResult:
    id: int = 0
    task_id: str = ""
    agent_id: str = ""
    status: str = ""
    progress: int = 0
    message: Optional[str] = None
    error_code: Optional[str] = None
    queued_at: str = ""
    delivered: bool = False
    attempt_id: Optional[str] = None
    result_json: Optional[str] = None
    error_message: Optional[str] = None
    retryable: int = 0
    recovery_action: Optional[str] = None
    delivery_attempts: int = 0
    delivered_at: Optional[str] = None
