"""Scoped checkpoint access. Publication side effects belong to PublicationJournal."""
from dataclasses import asdict, fields
from typing import Callable
from sqlalchemy import select, delete, or_
from sqlalchemy.dialects.sqlite import insert
from wt_media_agent.runtime.constants import CLOUD_ENVS, DEFAULT_CLOUD_ENV, TASK_TYPE_MATERIAL_DOWNLOAD
from wt_media_agent.storage.database import Database
from wt_media_agent.storage.models import TaskCheckpointModel as Checkpoint, TaskResultModel as Result
from wt_media_agent.storage.records import TaskCheckpoint, OfflineResult
from wt_media_agent.utils.time import utc_now_iso

class CheckpointStore:
    def __init__(self, database: Database, *, cloud_env: str = DEFAULT_CLOUD_ENV, user_id: Callable[[], str] = lambda: ''):
        if cloud_env not in CLOUD_ENVS:
            raise ValueError('invalid Cloud environment')
        self.database = database
        self._db_path = database.path
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
        if not self._db_path.exists():
            raise FileNotFoundError('database does not exist')
        with self.database.session() as session:
            session.execute(select(Checkpoint.id).limit(1)).first()

    def save_checkpoint(self, cp: TaskCheckpoint):
        if cp.task_type == 'publication':
            raise ValueError('publication writes require the immutable journal')
        values = asdict(cp)
        values.update(cloud_env=self.cloud_env, user_id=self.user_id(), created_at=cp.created_at or utc_now_iso(), updated_at=utc_now_iso())
        # Download checkpoints intentionally survive a replacement lease.
        values['attempt_id'] = None
        statement = insert(Checkpoint).values(**values)
        changes = {key:value for key,value in values.items() if key not in ('created_at','cloud_env','user_id','task_type','task_id','attempt_id')}
        statement = statement.on_conflict_do_update(index_elements=['cloud_env','user_id','task_type','task_id'],
            index_where=Checkpoint.attempt_id.is_(None), set_=changes)
        with self.database.session.begin() as session:
            session.execute(statement)

    def get_checkpoint(self, task_id: str, *, task_type=TASK_TYPE_MATERIAL_DOWNLOAD):
        with self.database.session() as session:
            row = session.scalar(select(Checkpoint).where(*self._scope(task_type), Checkpoint.task_id==task_id, Checkpoint.attempt_id.is_(None)))
            return None if row is None else self._record(row)

    def get_open_checkpoints(self):
        with self.database.session() as session:
            rows = session.scalars(select(Checkpoint).where(*self._scope(), Checkpoint.checkpoint_status.in_(['claimed','running'])).order_by(Checkpoint.updated_at)).all()
            return [self._record(row) for row in rows]

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

    def get_undelivered_results(self):
        with self.database.session() as session:
            rows=session.execute(select(Result,Checkpoint).join(Checkpoint,Result.checkpoint_id==Checkpoint.id).where(
                Checkpoint.cloud_env==self.cloud_env,Checkpoint.user_id==self.user_id(),Result.delivered.is_(False)).order_by(Result.id)).all()
            return [OfflineResult(**dict({f.name:getattr(result,f.name) for f in fields(OfflineResult) if hasattr(result,f.name)},
                task_id=cp.task_id,agent_id=cp.agent_id,attempt_id=cp.attempt_id)) for result,cp in rows]
