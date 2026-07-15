from __future__ import annotations

import argparse
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path


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
)


def default_data_dir() -> Path:
    configured = os.environ.get("WT_MEDIA_AGENT_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".wt-media-agent"


def default_db_path(data_dir: Path | None = None) -> Path:
    return (data_dir or default_data_dir()) / DEFAULT_DB_NAME


def apply_migrations(db_path: str | Path) -> list[AppliedMigration]:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
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
    for item in applied:
        print(f"applied {item.version} {item.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
