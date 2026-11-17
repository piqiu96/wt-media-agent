"""SQLite connection policy for the Agent's own local databases.

One place decides how this process opens its SQLite files. Two properties are
load-bearing:

- the row factory, because `CheckpointStore` builds its dataclasses from
  `dict(row)`;
- WAL, which is what lets the local API read while the runner writes.

`storage/migration.py` deliberately keeps its own bare `sqlite3.connect`. It
runs before any schema exists and only executes DDL, so it needs neither
property -- and its module path and symbols are frozen by CHG-056, so it is not
rewritten here to route through this function.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open `db_path` with the Agent's standard connection policy.

    The caller owns the returned connection and must close it; the `with`
    form commits but does not close, exactly as with `sqlite3.connect`.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # First open can race another connection changing rollback journal to
        # WAL. SQLite may return BUSY here without invoking its busy timeout.
        # Retry only this connection setup, never a task transaction or click.
        deadline = time.monotonic() + 5.0
        while True:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                return conn
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", 0) & 255
                if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
    except BaseException:
        conn.close()
        raise
