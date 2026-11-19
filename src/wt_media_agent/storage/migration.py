from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from wt_media_agent.runtime.config import get_config


from sqlalchemy import select
from sqlalchemy.orm import Session
from wt_media_agent.runtime.constants import DEFAULT_DB_NAME, CLOUD_ENVS
from wt_media_agent.storage.database import Database
from wt_media_agent.storage.models import SchemaMigration, TaskCheckpointModel, TaskResultModel, AgentMetadata
from wt_media_agent.utils.time import utc_now_iso

class LegacyIdentityRequired(RuntimeError):
    """An operator must identify historical records before they can be upgraded."""



@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    statements: tuple[str, ...]


@dataclass(frozen=True)
class AppliedMigration:
    version: str
    name: str


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


def default_data_dir() -> Path:
    """The local Agent data directory.

    Delegates to the configuration layer so there is one rule, not two
    (CHG-056 T-03). Signature and module path are deliberately unchanged:
    `pyproject.toml`'s `wt-media-agent-storage-migrate` console script points at
    `main` in this module, as does `scripts/migrate-storage.sh`.

    Behaviour change: with no `--data-dir`, no `--db-path` and no
    `WT_MEDIA_AGENT_DATA_DIR`, this used to return `~/.wt-media-agent`
    unconditionally. It now returns the deployment-shaped default -- `<repo>/
    .local/data` in a checkout, the installed location under
    `~/Library/Application Support/WTMedia/Agent` when frozen or in production.
    Any development database left in `~/.wt-media-agent` is therefore not
    migrated; pass `--db-path` to reach one.
    """
    return get_config().paths.data_dir


def default_db_path(data_dir: Path | None = None) -> Path:
    return (data_dir or default_data_dir()) / DEFAULT_DB_NAME


def _encode(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def _upgrade(connection, *, legacy_scope, cloud_env_map):
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


def apply_migrations(database: Database | str | Path, *, legacy_scope: tuple[str,str] | None = None,
                     cloud_env_map: dict[str,str] | None = None) -> list[AppliedMigration]:
    if legacy_scope and (legacy_scope[0] not in CLOUD_ENVS or not legacy_scope[1]):
        raise ValueError('verified legacy environment and user required')
    cloud_env_map=cloud_env_map or {}
    if any(env not in CLOUD_ENVS for env in cloud_env_map.values()):
        raise ValueError('invalid legacy Cloud environment mapping')
    owned=not isinstance(database,Database)
    database=Database(database) if owned else database
    try:
        with database.engine.begin() as connection:
            SchemaMigration.__table__.create(connection,checkfirst=True)
            versions=set(connection.execute(select(SchemaMigration.version)).scalars())
            applied=[]
            for item in MIGRATIONS:
                if item.version in versions:
                    continue
                if item.version=='0005_scoped_orm':
                    _upgrade(connection,legacy_scope=legacy_scope,cloud_env_map=cloud_env_map)
                else:
                    for statement in item.statements:
                        connection.exec_driver_sql(statement)
                connection.execute(SchemaMigration.__table__.insert().values(version=item.version,name=item.name))
                applied.append(AppliedMigration(item.version,item.name))
            return applied
    finally:
        if owned:
            database.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", help="local Agent data directory")
    parser.add_argument("--db-path", help="explicit SQLite database path")
    parser.add_argument('--legacy-cloud-env', choices=sorted(CLOUD_ENVS))
    parser.add_argument('--legacy-user-id', help='verified owner of historical download/claim records')
    parser.add_argument('--legacy-cloud-url', help='verified old Cloud URL for publication/claim mapping')
    args = parser.parse_args(argv)
    if bool(args.legacy_cloud_env) != bool(args.legacy_user_id):
        parser.error('--legacy-cloud-env and --legacy-user-id must be supplied together')

    db_path = Path(args.db_path) if args.db_path else default_db_path(
        Path(args.data_dir).expanduser() if args.data_dir else None
    )
    config=get_config()
    with_database=Database(db_path,synchronous=config.database_synchronous,timeout_seconds=config.database_timeout_seconds)
    try:
        applied = apply_migrations(with_database,
            legacy_scope=(args.legacy_cloud_env,args.legacy_user_id) if args.legacy_user_id else None,
            cloud_env_map={args.legacy_cloud_url:args.legacy_cloud_env} if args.legacy_cloud_url else {})
    finally:
        with_database.close()
    print(f"storage migration ok: {len(applied)} applied, {len(MIGRATIONS)} total")
    print(f"database {db_path}")
    for item in applied:
        print(f"applied {item.version} {item.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
