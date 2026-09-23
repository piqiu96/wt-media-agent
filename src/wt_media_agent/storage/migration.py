from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from wt_media_agent.runtime.config import get_config


DEFAULT_DB_NAME = "local-agent.sqlite3"


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


def apply_migrations(db_path: str | Path) -> list[AppliedMigration]:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # `closing` for the connection, `db` for the transaction: `with connection`
    # alone commits but never closes, so this leaked one connection per call.
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT NOT NULL PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """
        )
        applied_versions = {
            row[0] for row in db.execute("SELECT version FROM schema_migrations")
        }
        applied: list[AppliedMigration] = []
        for item in MIGRATIONS:
            if item.version in applied_versions:
                continue
            with db:
                for statement in item.statements:
                    db.execute(statement)
                db.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                    (item.version, item.name),
                )
            applied.append(AppliedMigration(version=item.version, name=item.name))
        return applied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", help="local Agent data directory")
    parser.add_argument("--db-path", help="explicit SQLite database path")
    args = parser.parse_args(argv)

    db_path = Path(args.db_path) if args.db_path else default_db_path(
        Path(args.data_dir).expanduser() if args.data_dir else None
    )
    applied = apply_migrations(db_path)
    print(f"storage migration ok: {len(applied)} applied, {len(MIGRATIONS)} total")
    print(f"database {db_path}")
    for item in applied:
        print(f"applied {item.version} {item.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
