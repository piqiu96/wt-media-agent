"""Durable publication boundary, using task checkpoints and immutable result records.

These are local records, not a second Cloud task model or a wire contract.
A report acknowledgement never authorizes another platform click.
"""
from __future__ import annotations

import hashlib
import json
from sqlalchemy import select, update
from wt_media_agent.runtime.constants import CLOUD_ENVS, TASK_TYPE_PUBLICATION, PublicationCheckpointStatus, PublicationPhase, PUBLICATION_LOCAL_RESULT_STATUSES, PUBLICATION_SETTLED_STATUSES
from wt_media_agent.storage.database import Database, retry_write
from wt_media_agent.storage.models import TaskCheckpointModel as Checkpoint, TaskResultModel as Result
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from wt_media_agent.utils.time import utc_now_iso


class JournalConflict(RuntimeError):
    """An immutable input, active profile or result would be overwritten."""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _environment(value: str) -> str:
    if value not in CLOUD_ENVS:
        raise ValueError('explicit Cloud environment required: dev/test/pre/prod')
    return value



@dataclass(frozen=True)
class ExecutionIdentity:
    cloud_env: str
    user_id: str
    task_id: str
    attempt_id: str
    bit_profile_id: str

    def __post_init__(self):
        object.__setattr__(self, 'cloud_env', _environment(self.cloud_env))
        for value in (self.user_id, self.task_id, self.attempt_id):
            if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or str(int(value)) != value or not 0 < int(value) < 2**64:
                raise ValueError('positive canonical decimal identities are required')
        if not isinstance(self.bit_profile_id, str) or not self.bit_profile_id or len(self.bit_profile_id) > 255 or self.bit_profile_id.strip() != self.bit_profile_id:
            raise ValueError('actual BitBrowser Profile identity is required')

    @property
    def key(self) -> str:
        # Only an in-process/filesystem identifier; the database stores separate columns.
        return f'{self.cloud_env}-{self.user_id}-{self.task_id}-{self.attempt_id}'



def _validated_snapshot(value: dict) -> str:
    required = {'platform', 'platform_uid', 'video_sha256', 'video_size', 'cover_sha256', 'cover_size', 'content'}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError('only immutable public execution inputs may be saved')
    if value['platform'] not in ('baidu', 'bilibili', 'xiaohongshu') or not isinstance(value['platform_uid'], str) or not value['platform_uid']:
        raise ValueError('platform identity is required')
    for name in ('video_sha256', 'cover_sha256'):
        digest = value[name]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('file digest is required')
    for name in ('video_size', 'cover_size'):
        if type(value[name]) is not int or value[name] <= 0:
            raise ValueError('positive file size is required')
    content = value['content']
    if not isinstance(content, dict) or set(content) != {'description', 'topics', 'declaration'} or any(not isinstance(v, str) for v in content.values()):
        raise ValueError('only confirmed text fields may be saved')
    return _json(value)


def _safe_result(value: dict) -> None:
    if not isinstance(value, dict) or value.get('status') not in PUBLICATION_LOCAL_RESULT_STATUSES:
        raise ValueError('an explicit observation is required')
    def visit(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if not isinstance(key, str) or any(part in key.lower() for part in ('token', 'cookie', 'password', 'authorization', 'download_url', 'signed_url')):
                    raise ValueError('credentials and signed addresses cannot enter evidence')
                visit(child)
        elif isinstance(node, list):
            for child in node: visit(child)
        elif isinstance(node, str) and node.startswith(('https://', 'http://')):
            url = urlsplit(node)
            if url.username or url.password or url.query or url.fragment:
                raise ValueError('only public credential-free evidence URLs may be saved')
    visit(value)


class PublicationJournal:
    def __init__(self, database: Database, *, agent_id: str = ''):
        self.database = database
        self.agent_id = agent_id

    @staticmethod
    def _scope(identity):
        return (Checkpoint.cloud_env==identity.cloud_env,Checkpoint.user_id==identity.user_id,
            Checkpoint.task_type==TASK_TYPE_PUBLICATION,Checkpoint.task_id==identity.task_id,Checkpoint.attempt_id==identity.attempt_id)

    @classmethod
    def _state(cls, session, identity):
        row=session.scalar(select(Checkpoint).where(*cls._scope(identity)))
        if row is None:
            raise JournalConflict('no durable publication claim')
        state=json.loads(row.checkpoint_json)
        if state['identity'] != asdict(identity):
            raise JournalConflict('attempt identity differs')
        return row,state

    @staticmethod
    def _write(session, row, state, **values):
        original=row.checkpoint_json
        result=session.execute(update(Checkpoint).where(Checkpoint.id==row.id,Checkpoint.checkpoint_json==original).values(
            checkpoint_json=_json(state),last_stage=state['phase'],updated_at=utc_now_iso(),**values),execution_options={'synchronize_session':False})
        if result.rowcount != 1:
            raise JournalConflict('execution state changed concurrently')

    def register(self, identity: ExecutionIdentity, frozen_input: dict) -> bool:
        encoded=_validated_snapshot(frozen_input)
        digest=hashlib.sha256(encoded.encode()).hexdigest()
        state={'identity':asdict(identity),'input':json.loads(encoded),'phase':PublicationPhase.CLAIMED,'report_seq':0}
        now=utc_now_iso()
        # Insert first: uniqueness is the arbiter, even with deferred transactions.
        with self.database.session.begin() as session:
            inserted=self.database.insert_if_absent(session, Checkpoint, dict(cloud_env=identity.cloud_env,user_id=identity.user_id,
                task_id=identity.task_id,task_type=TASK_TYPE_PUBLICATION,agent_id=self.agent_id,attempt_id=identity.attempt_id,
                bit_profile_id=identity.bit_profile_id,checkpoint_status=PublicationCheckpointStatus.OPEN,input_hash=digest,
                task_payload_json=encoded,checkpoint_json=_json(state),last_stage=PublicationPhase.CLAIMED,external_side_effect=1,
                created_at=now,updated_at=now))
            if inserted:
                return True
            old=session.scalar(select(Checkpoint).where(*self._scope(identity)))
            if old is None:
                raise JournalConflict('profile has an unresolved publication scene')
            self._state(session,identity)
            if old.input_hash != digest:
                raise JournalConflict('attempt input differs')
            return False

    def get(self, identity: ExecutionIdentity) -> dict:
        with self.database.session() as session:
            return self._state(session,identity)[1]

    @retry_write
    def advance(self, identity: ExecutionIdentity, phase: str) -> int:
        previous={PublicationPhase.PREPARED:PublicationPhase.CLAIMED,PublicationPhase.SUBMITTING:PublicationPhase.PREPARED,PublicationPhase.CLICK_STARTED:PublicationPhase.SUBMITTING}
        if phase not in previous:
            raise ValueError('unsupported local submit phase')
        with self.database.session.begin() as session:
            row,state=self._state(session,identity)
            if state['phase'] != previous[phase]:
                raise JournalConflict('submit boundary cannot be replayed or reversed')
            state.update(phase=phase,report_seq=state['report_seq']+1)
            self._write(session,row,state)
            return state['report_seq']

    @retry_write
    def save_result(self, identity: ExecutionIdentity, result: dict, *, report_seq: int) -> dict:
        _safe_result(result)
        if type(report_seq) is not int or report_seq <= 0:
            raise JournalConflict('result sequence must advance')
        digest=hashlib.sha256(_json({'report_seq':report_seq,'result':result}).encode()).hexdigest()
        packet={'task_id':identity.task_id,'attempt_id':identity.attempt_id,'report_seq':report_seq,'result_hash':digest,'result':result}
        with self.database.session.begin() as session:
            row,state=self._state(session,identity)
            prior=session.scalar(select(Result).where(Result.checkpoint_id==row.id))
            if prior:
                if json.loads(prior.result_json) != packet:
                    raise JournalConflict('saved execution result is immutable')
                return packet
            if report_seq <= state['report_seq'] or state['phase']==PublicationPhase.SETTLED:
                raise JournalConflict('result sequence is stale')
            state.update(phase=PublicationPhase.RESULT_SAVED,report_seq=report_seq,result_hash=digest)
            self._write(session,row,state,checkpoint_status=PublicationCheckpointStatus.RESULT,final_status=result['status'],report_pending=1,finished_at=utc_now_iso())
            session.add(Result(checkpoint_id=row.id,status=result['status'],result_json=_json(packet),result_hash=digest,queued_at=utc_now_iso()))
        return packet

    def pending(self, cloud_env: str, user_id: str) -> list[dict]:
        with self.database.session() as session:
            rows=session.scalars(select(Result).join(Checkpoint,Checkpoint.id==Result.checkpoint_id).where(
                Checkpoint.task_type==TASK_TYPE_PUBLICATION,Checkpoint.cloud_env==_environment(cloud_env),
                Checkpoint.user_id==user_id,Result.delivered.is_(False)).order_by(Result.id)).all()
            return [{'id':r.id,'packet':json.loads(r.result_json)} for r in rows]

    @retry_write
    def record_delivery(self, cloud_env: str, user_id: str, packet: dict, *, accepted: bool) -> bool:
        with self.database.session.begin() as session:
            row=session.scalar(select(Result).join(Checkpoint,Checkpoint.id==Result.checkpoint_id).where(
                Checkpoint.task_id==packet['task_id'],Checkpoint.attempt_id==packet['attempt_id'],
                Result.delivered.is_(False),Checkpoint.cloud_env==_environment(cloud_env),
                Checkpoint.user_id==user_id,Checkpoint.task_type==TASK_TYPE_PUBLICATION))
            if row is None:
                return False
            if json.loads(row.result_json) != packet:
                raise JournalConflict('pending result differs')
            row.delivery_attempts+=1
            if accepted:
                row.delivered=True
                row.delivered_at=utc_now_iso()
                session.execute(update(Checkpoint).where(Checkpoint.id==row.checkpoint_id).values(report_pending=0))
                return True
        return False

    @retry_write
    def settle(self, identity: ExecutionIdentity, result_hash: str, *, task_status: str):
        if task_status not in PUBLICATION_SETTLED_STATUSES:
            raise JournalConflict('Cloud has not resolved this execution')
        with self.database.session.begin() as session:
            row,state=self._state(session,identity)
            if not state.get('result_hash') or state['result_hash']!=result_hash or row.report_pending:
                raise JournalConflict('result must be durably acknowledged before resolution')
            state['phase']=PublicationPhase.SETTLED
            state.pop('input',None)
            self._write(session,row,state,checkpoint_status=PublicationCheckpointStatus.SETTLED,task_payload_json=None)
