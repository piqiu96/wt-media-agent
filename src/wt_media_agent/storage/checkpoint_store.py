"""Scoped checkpoint access. Publication side effects belong to PublicationJournal."""
from dataclasses import asdict, fields, dataclass
from typing import Callable, Optional
from sqlalchemy import select, delete, or_, func
from wt_media_agent.runtime.constants import CLOUD_ENVS, DEFAULT_CLOUD_ENV, TASK_TYPE_MATERIAL_DOWNLOAD, DEFAULT_EXECUTION_TIMEOUT_SECONDS
from wt_media_agent.storage.database import Database
from wt_media_agent.storage.models import TaskCheckpointModel as Checkpoint, TaskResultModel as Result
from wt_media_agent.utils.time import utc_now_iso

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
    execution_timeout_seconds: int = DEFAULT_EXECUTION_TIMEOUT_SECONDS
    external_side_effect: int = 0

class CheckpointStore:
    def __init__(self, database: Database, *, cloud_env: str = DEFAULT_CLOUD_ENV, user_id: Callable[[], str] = lambda: ''):
        if cloud_env not in CLOUD_ENVS:
            raise ValueError('invalid Cloud environment')
        self.database = database
        self.cloud_env = cloud_env
        self.user_id = user_id

    def for_user(self, user_id: str):
        return CheckpointStore(self.database, cloud_env=self.cloud_env, user_id=lambda:user_id)

    def _scope(self, task_type=TASK_TYPE_MATERIAL_DOWNLOAD):
        return (Checkpoint.cloud_env==self.cloud_env, Checkpoint.user_id==self.user_id(), Checkpoint.task_type==task_type)

    @staticmethod
    def _record(row):
        return TaskCheckpoint(**{f.name:getattr(row,f.name) for f in fields(TaskCheckpoint)})

    def probe(self):
        self.database.probe(Checkpoint)

    def save_checkpoint(self, cp: TaskCheckpoint):
        if cp.task_type == 'publication':
            raise ValueError('publication writes require the immutable journal')
        values = asdict(cp)
        values.update(cloud_env=self.cloud_env, user_id=self.user_id(), created_at=cp.created_at or utc_now_iso(), updated_at=utc_now_iso())
        # Download checkpoints intentionally survive a replacement lease.
        values['attempt_id'] = None
        changes = {key:value for key,value in values.items() if key not in ('created_at','cloud_env','user_id','task_type','task_id','attempt_id')}
        with self.database.session.begin() as session:
            self.database.upsert(session, Checkpoint, values, changes, unique='uq_checkpoint_task')

    def get_checkpoint(self, task_id: str, *, task_type=TASK_TYPE_MATERIAL_DOWNLOAD):
        with self.database.session() as session:
            row = session.scalar(select(Checkpoint).where(*self._scope(task_type), Checkpoint.task_id==task_id, Checkpoint.attempt_id.is_(None)))
            return None if row is None else self._record(row)

    def get_incomplete_checkpoints(self):
        with self.database.session() as session:
            rows=session.scalars(select(Checkpoint).where(Checkpoint.cloud_env==self.cloud_env, Checkpoint.user_id==self.user_id(),
                or_(Checkpoint.checkpoint_status.in_(['claimed','running']),Checkpoint.report_pending==1)).order_by(Checkpoint.updated_at)).all()
            return [self._record(row) for row in rows]

    def remove_checkpoint(self, task_id: str, *, task_type=TASK_TYPE_MATERIAL_DOWNLOAD):
        if task_type == 'publication':
            raise ValueError('publication replay barriers cannot be deleted')
        with self.database.session.begin() as session:
            session.execute(delete(Checkpoint).where(*self._scope(task_type), Checkpoint.task_id==task_id, Checkpoint.attempt_id.is_(None)))

    def count_undelivered_results(self):
        with self.database.session() as session:
            return session.scalar(select(func.count()).select_from(Result).join(Checkpoint,Result.checkpoint_id==Checkpoint.id).where(
                Checkpoint.cloud_env==self.cloud_env,Checkpoint.user_id==self.user_id(),Result.delivered.is_(False)))
