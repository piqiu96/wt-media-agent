"""The local storage schema. Business IDs never double as local primary keys."""
from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from wt_media_agent.runtime.constants import TASK_TYPE_PUBLICATION, PublicationCheckpointStatus, DEFAULT_EXECUTION_TIMEOUT_SECONDS
from wt_media_agent.utils.time import utc_now_iso

class Base(DeclarativeBase):
    pass

class AgentMetadata(Base):
    __tablename__ = 'agent_metadata'
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)

class SchemaMigration(Base):
    __tablename__ = 'schema_migrations'
    version: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    applied_at: Mapped[str] = mapped_column(Text, default=utc_now_iso)

class TaskCheckpointModel(Base):
    __tablename__ = 'task_checkpoint'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cloud_env: Mapped[str] = mapped_column(String(8))
    user_id: Mapped[str] = mapped_column(Text)
    task_id: Mapped[str] = mapped_column(Text)
    task_type: Mapped[str] = mapped_column(Text)
    agent_id: Mapped[str] = mapped_column(Text)
    attempt_id: Mapped[str | None] = mapped_column(Text)
    bit_profile_id: Mapped[str | None] = mapped_column(Text)
    checkpoint_status: Mapped[str] = mapped_column(Text)
    input_hash: Mapped[str | None] = mapped_column(Text)
    progress: Mapped[int] = mapped_column(default=0, server_default=text('0'))
    message: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(default=0, server_default=text('0'))
    transfer_state_json: Mapped[str | None] = mapped_column(Text)
    task_payload_json: Mapped[str | None] = mapped_column(Text)
    last_stage: Mapped[str | None] = mapped_column(Text)
    checkpoint_json: Mapped[str | None] = mapped_column(Text)
    final_status: Mapped[str | None] = mapped_column(Text)
    final_result_json: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    report_pending: Mapped[int] = mapped_column(default=0, server_default=text('0'))
    last_report_at: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)
    execution_timeout_seconds: Mapped[int] = mapped_column(default=DEFAULT_EXECUTION_TIMEOUT_SECONDS, server_default=text(str(DEFAULT_EXECUTION_TIMEOUT_SECONDS)))
    external_side_effect: Mapped[int] = mapped_column(default=0, server_default=text('0'))
    __table_args__ = {'info': {'unique_rules': (
        {'name': 'uq_checkpoint_attempt', 'columns': ('cloud_env','user_id','task_type','task_id','attempt_id'), 'where': text('attempt_id IS NOT NULL')},
        {'name': 'uq_checkpoint_task', 'columns': ('cloud_env','user_id','task_type','task_id'), 'where': text('attempt_id IS NULL')},
        {'name': 'uq_publication_profile', 'columns': ('bit_profile_id',), 'where': text(f"task_type='{TASK_TYPE_PUBLICATION}' AND checkpoint_status<>'{PublicationCheckpointStatus.SETTLED}'")},
        {'name': 'uq_publication_attempt', 'columns': ('cloud_env','user_id','attempt_id'), 'where': text(f"task_type='{TASK_TYPE_PUBLICATION}'")},
    )}}


class TaskResultModel(Base):
    __tablename__ = 'task_result'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    checkpoint_id: Mapped[int] = mapped_column(ForeignKey('task_checkpoint.id', ondelete='RESTRICT'), unique=True)
    status: Mapped[str] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text)
    result_hash: Mapped[str | None] = mapped_column(Text)
    progress: Mapped[int] = mapped_column(default=0, server_default=text('0'))
    message: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    retryable: Mapped[int] = mapped_column(default=0, server_default=text('0'))
    recovery_action: Mapped[str | None] = mapped_column(Text)
    queued_at: Mapped[str] = mapped_column(Text)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text('0'))
    delivery_attempts: Mapped[int] = mapped_column(default=0, server_default=text('0'))
    delivered_at: Mapped[str | None] = mapped_column(Text)
