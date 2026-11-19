"""Process-owned SQLAlchemy resources; one Session per operation."""
from pathlib import Path
from functools import wraps
from sqlalchemy.exc import OperationalError
import sqlite3
import time
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from wt_media_agent.runtime.constants import DEFAULT_DATABASE_SYNCHRONOUS, DEFAULT_DATABASE_TIMEOUT_SECONDS

class Database:
    def __init__(self, path: str | Path, *, synchronous: str = DEFAULT_DATABASE_SYNCHRONOUS,
                 timeout_seconds: float = DEFAULT_DATABASE_TIMEOUT_SECONDS):
        if synchronous not in {'FULL', 'NORMAL', 'EXTRA'}:
            raise ValueError('unsupported database synchronous policy')
        if timeout_seconds <= 0:
            raise ValueError('database timeout must be positive')
        self.timeout_seconds = timeout_seconds
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(URL.create('sqlite', database=str(self.path)),
            connect_args={'autocommit': False, 'timeout': timeout_seconds}, poolclass=NullPool)

        @event.listens_for(self.engine, 'connect')
        def initialize(connection, _record):
            # sqlite3 >=3.12: PRAGMA connection settings must run outside a transaction.
            previous = connection.autocommit
            connection.autocommit = True
            cursor = connection.cursor()
            try:
                cursor.execute('PRAGMA foreign_keys=ON')
                cursor.execute('PRAGMA synchronous=' + synchronous)
                deadline = time.monotonic() + timeout_seconds
                while True:
                    try:
                        cursor.execute('PRAGMA journal_mode=WAL')
                        break
                    except sqlite3.OperationalError as error:
                        if (getattr(error, 'sqlite_errorcode', 0) & 255) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() >= deadline:
                            raise
                        time.sleep(min(0.01, max(0, deadline-time.monotonic())))
            finally:
                cursor.close()
                connection.autocommit = previous
        self.session = sessionmaker(self.engine, expire_on_commit=False)

    def close(self):
        self.engine.dispose()


def retry_sqlite_write(operation):
    """Retry a short database-only operation after a stale read snapshot.

    The decorated operation must create/close its Session on every invocation.
    Never apply this to browser, file or network operations (including flush).
    """
    @wraps(operation)
    def write(self, *args, **kwargs):
        deadline = time.monotonic() + self.database.timeout_seconds
        while True:
            try:
                return operation(self, *args, **kwargs)
            except OperationalError as error:
                code = getattr(error.orig, 'sqlite_errorcode', 0) & 255
                if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() >= deadline:
                    raise
                # The failed Session has exited and rolled back before this wait.
                time.sleep(min(0.01, max(0, deadline-time.monotonic())))
    return write
