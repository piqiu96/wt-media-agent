"""SQLite mechanics; stores express rules without importing this dialect."""
import hashlib
import json
import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from sqlalchemy import Index, MetaData, event, select
from sqlalchemy.engine import URL
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from wt_media_agent.runtime.constants import CLOUD_ENVS, DATABASE_RETRY_INTERVAL_SECONDS
from wt_media_agent.storage.models import AgentMetadata, TaskCheckpointModel, TaskResultModel
from wt_media_agent.utils.time import utc_now_iso

class LegacyIdentityRequired(RuntimeError):
    """An operator must identify historical records before they can be upgraded."""



@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version="0001_base",
        name="base",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS agent_metadata (
                key TEXT NOT NULL PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        ),
    ),
    Migration(
        version="0002_task_checkpoints",
        name="task_checkpoints",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS task_checkpoints (
                task_id TEXT NOT NULL PRIMARY KEY,
                task_type TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                checkpoint_status TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                message TEXT DEFAULT NULL,
                result_json TEXT DEFAULT NULL,
                error_code TEXT DEFAULT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                retry_count INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS offline_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                status TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                message TEXT DEFAULT NULL,
                error_code TEXT DEFAULT NULL,
                queued_at TEXT NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0
            )
            """,
        ),
    ),
    # Appended, never folded into 0002. A migration that has been applied is
    # skipped by version (`apply_migrations`), so editing 0002 to add this column
    # would reach only databases created after the edit and leave every existing
    # one silently short a column -- and `get_checkpoint` reads `SELECT *` into a
    # dataclass, so the symptom would be a `TypeError` on the operator's machine
    # and nothing here.
    Migration(
        version="0003_transfer_resume",
        name="transfer_resume",
        statements=(
            """
            ALTER TABLE task_checkpoints
                ADD COLUMN transfer_state_json TEXT DEFAULT NULL
            """,
        ),
    ),
    # 终态必须先落盘再上报，所以 checkpoint 记录完整的任务上下文与终态，
    # offline_results 保存上报所需的全部字段（含 result 与错误元数据）。
    Migration(
        version="0004_task_recovery_outbox",
        name="task_recovery_outbox",
        statements=(
            """
            ALTER TABLE task_checkpoints
                ADD COLUMN attempt_id TEXT DEFAULT NULL
            """,
            """
            ALTER TABLE task_checkpoints
                ADD COLUMN task_payload_json TEXT DEFAULT NULL
            """,
            """
            ALTER TABLE task_checkpoints
                ADD COLUMN last_stage TEXT DEFAULT NULL
            """,
            """
            ALTER TABLE task_checkpoints
                ADD COLUMN checkpoint_json TEXT DEFAULT NULL
            """,
            """
            ALTER TABLE task_checkpoints
                ADD COLUMN final_status TEXT DEFAULT NULL
            """,
            """
            ALTER TABLE task_checkpoints
                ADD COLUMN final_result_json TEXT DEFAULT NULL
            """,
            """
            ALTER TABLE task_checkpoints
                ADD COLUMN error_message TEXT DEFAULT NULL
            """,
            """
            ALTER TABLE task_checkpoints
                ADD COLUMN report_pending INTEGER NOT NULL DEFAULT 0
            """,
            """
            ALTER TABLE task_checkpoints
                ADD COLUMN last_report_at TEXT DEFAULT NULL
            """,
            """
            ALTER TABLE task_checkpoints
                ADD COLUMN started_at TEXT DEFAULT NULL
            """,
            """
            ALTER TABLE task_checkpoints
                ADD COLUMN finished_at TEXT DEFAULT NULL
            """,
            """
            ALTER TABLE task_checkpoints
                ADD COLUMN execution_timeout_seconds INTEGER NOT NULL DEFAULT 300
            """,
            """
            ALTER TABLE task_checkpoints
                ADD COLUMN external_side_effect INTEGER NOT NULL DEFAULT 0
            """,
            """
            ALTER TABLE offline_results
                ADD COLUMN attempt_id TEXT DEFAULT NULL
            """,
            """
            ALTER TABLE offline_results
                ADD COLUMN result_json TEXT DEFAULT NULL
            """,
            """
            ALTER TABLE offline_results
                ADD COLUMN error_message TEXT DEFAULT NULL
            """,
            """
            ALTER TABLE offline_results
                ADD COLUMN retryable INTEGER NOT NULL DEFAULT 0
            """,
            """
            ALTER TABLE offline_results
                ADD COLUMN recovery_action TEXT DEFAULT NULL
            """,
            """
            ALTER TABLE offline_results
                ADD COLUMN delivery_attempts INTEGER NOT NULL DEFAULT 0
            """,
            """
            ALTER TABLE offline_results
                ADD COLUMN delivered_at TEXT DEFAULT NULL
            """,
        ),
    ),
    Migration('0005_scoped_orm', 'scoped_orm', ()),
)



class SQLiteDialect:
    migrations = MIGRATIONS

    def __init__(self, path, *, synchronous, timeout_seconds):
        if synchronous not in {'FULL', 'NORMAL', 'EXTRA'}:
            raise ValueError('unsupported database synchronous policy')
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError('database timeout must be positive and finite')
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = timeout_seconds
        self.synchronous = synchronous
        self.url = URL.create('sqlite', database=str(self.path))
        self.engine_options = {'connect_args': {'autocommit': False, 'timeout': timeout_seconds}, 'poolclass': NullPool}

    @staticmethod
    def is_retryable(error):
        return (getattr(error, 'sqlite_errorcode', 0) & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)

    def configure(self, engine):
        @event.listens_for(engine, 'connect')
        def initialize(connection, _record):
            previous = connection.autocommit
            connection.autocommit = True
            cursor = connection.cursor()
            try:
                cursor.execute('PRAGMA foreign_keys=ON')
                cursor.execute('PRAGMA synchronous=' + self.synchronous)
                deadline = time.monotonic() + self.timeout_seconds
                while True:
                    try:
                        cursor.execute('PRAGMA journal_mode=WAL')
                        break
                    except sqlite3.OperationalError as error:
                        if not self.is_retryable(error) or time.monotonic() >= deadline:
                            raise
                        time.sleep(min(DATABASE_RETRY_INTERVAL_SECONDS, max(0, deadline-time.monotonic())))
            finally:
                cursor.close()
                connection.autocommit = previous

    def check_available(self):
        if not self.path.exists():
            raise FileNotFoundError('database does not exist')

    @staticmethod
    def insert_if_absent(session, model, values):
        return session.execute(insert(model).values(**values).on_conflict_do_nothing()).rowcount == 1

    @staticmethod
    def upsert(session, model, values, changes, *, unique=None):
        table = model.__table__
        if unique is None:
            columns, condition = tuple(table.primary_key.columns), None
        else:
            rule = next(rule for rule in table.info['unique_rules'] if rule['name'] == unique)
            columns, condition = rule['columns'], rule['where']
        statement = insert(model).values(**values)
        session.execute(statement.on_conflict_do_update(index_elements=columns, index_where=condition, set_=changes))

    @staticmethod
    def create_constraints(connection, table):
        # Clone metadata: concurrent migrations must not mutate global ORM tables.
        local_table = table.to_metadata(MetaData())
        for rule in table.info.get('unique_rules', ()):
            Index(rule['name'], *(local_table.c[name] for name in rule['columns']),
                  unique=True, sqlite_where=rule['where']).create(connection, checkfirst=True)

    def apply_migration(self, connection, item, *, legacy_scope, cloud_env_map):
        if item.version == '0005_scoped_orm':
            _upgrade(connection, legacy_scope=legacy_scope, cloud_env_map=cloud_env_map,
                     create_constraints=self.create_constraints)
        else:
            for statement in item.statements:
                connection.exec_driver_sql(statement)


def _encode(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def _upgrade(connection, *, legacy_scope, cloud_env_map, create_constraints):
    old_checkpoints=connection.exec_driver_sql('SELECT * FROM task_checkpoints').mappings().all()
    old_results=connection.exec_driver_sql('SELECT * FROM offline_results').mappings().all()
    converted=[]
    users=set()
    for original in old_checkpoints:
        values=dict(original)
        if values['task_type']=='publication':
            state=json.loads(values['checkpoint_json'])
            identity=state['identity']
            old_env=identity.get('cloud_env',identity.get('environment'))
            env=old_env if old_env in CLOUD_ENVS else cloud_env_map.get(old_env)
            if env not in CLOUD_ENVS:
                raise LegacyIdentityRequired('historical publication requires an explicit Cloud URL/environment mapping; original database unchanged')
            user=identity['user_id']
            values.update(task_id=identity['task_id'],attempt_id=identity['attempt_id'],bit_profile_id=identity['bit_profile_id'],
                input_hash=hashlib.sha256(_encode(state['input']).encode()).hexdigest())
            identity.pop('environment',None)
            identity['cloud_env']=env
            values['checkpoint_json']=_encode(state)
            # Historical agent_id held user_id. Do not invent a device identity.
            values['agent_id']=''
        else:
            if legacy_scope is None:
                raise LegacyIdentityRequired('historical download owner/environment is unknown; use --legacy-cloud-env and --legacy-user-id after verification; original database unchanged')
            env,user=legacy_scope
            values['attempt_id']=None
        users.add(user)
        values.update(cloud_env=env,user_id=user)
        converted.append((original['task_id'],values))
    # Old claim keys contain an irreversible URL/user digest. Only verified
    # mappings can match one; never rotate an unresolved request on upgrade.
    metadata=connection.exec_driver_sql("SELECT key,value FROM agent_metadata WHERE key LIKE 'publication.claim.%'").mappings().all()
    if legacy_scope:
        users.add(legacy_scope[1])
    claim_changes=[]
    for entry in metadata:
        if len(entry['key'].removeprefix('publication.claim.')) != 64:
            continue
        matches=[]
        for url,env in cloud_env_map.items():
            for user in users:
                raw=json.dumps([url,user],separators=(',',':'))
                if entry['key']=='publication.claim.'+hashlib.sha256(raw.encode()).hexdigest():
                    matches.append(f'publication.claim.{env}.{user}')
        if len(set(matches)) != 1:
            raise LegacyIdentityRequired('pending historical claim requires a verified Cloud URL and user mapping; original request key retained')
        claim_changes.append((entry,matches[0]))
    TaskCheckpointModel.__table__.create(connection,checkfirst=True)
    TaskResultModel.__table__.create(connection,checkfirst=True)
    create_constraints(connection, TaskCheckpointModel.__table__)
    # Bind the ORM to this migration transaction; it must not commit independently.
    with Session(bind=connection,join_transaction_mode='rollback_only') as session:
        mapping={}
        for old_id,values in converted:
            row=TaskCheckpointModel(**values)
            session.add(row)
            session.flush()
            mapping[old_id]=row.id
        for original in old_results:
            values=dict(original)
            old_id=values.pop('task_id')
            cp_id=mapping.get(old_id)
            if cp_id is None:
                raise LegacyIdentityRequired('orphaned historical result requires reconciliation; original database unchanged')
            values.pop('agent_id',None)
            values.pop('attempt_id',None)
            values.update(checkpoint_id=cp_id)
            packet=json.loads(values['result_json']) if values.get('result_json') else {}
            values['result_hash']=packet.get('result_hash')
            session.add(TaskResultModel(**values))
        for entry,new_key in claim_changes:
            if session.get(AgentMetadata,new_key) is not None:
                raise LegacyIdentityRequired('historical claim conflicts with an existing pending request')
            session.add(AgentMetadata(key=new_key,value=entry['value'],updated_at=utc_now_iso()))
            session.delete(session.get(AgentMetadata,entry['key']))
        session.flush()
    connection.exec_driver_sql('DROP TABLE offline_results')
    connection.exec_driver_sql('DROP TABLE task_checkpoints')


