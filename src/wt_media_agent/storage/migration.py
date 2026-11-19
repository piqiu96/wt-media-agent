"""Stable migration entrypoint; the selected driver owns its upgrade steps."""
from __future__ import annotations
import argparse
from dataclasses import dataclass
from pathlib import Path
from sqlalchemy import select
from wt_media_agent.runtime.config import get_config
from wt_media_agent.runtime.constants import DEFAULT_DB_NAME, CLOUD_ENVS
from wt_media_agent.storage.database import Database
from wt_media_agent.storage.models import SchemaMigration
# Public legacy migration fixtures remain importable at their existing path.
from wt_media_agent.storage.dialect import MIGRATIONS, Migration, LegacyIdentityRequired

@dataclass(frozen=True)
class AppliedMigration:
    version: str
    name: str

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
            for item in database.migrations:
                if item.version in versions:
                    continue
                database.apply_migration(connection, item, legacy_scope=legacy_scope, cloud_env_map=cloud_env_map)
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
    print(f"storage migration ok: {len(applied)} applied, {len(with_database.migrations)} total")
    print(f"database {db_path}")
    for item in applied:
        print(f"applied {item.version} {item.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
