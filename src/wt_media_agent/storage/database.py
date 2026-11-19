"""Process-owned ORM resources; database selection stays inside this module."""
from pathlib import Path
from functools import wraps
import time
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from wt_media_agent.runtime.constants import DEFAULT_DB_NAME, DEFAULT_DATABASE_SYNCHRONOUS, DEFAULT_DATABASE_TIMEOUT_SECONDS, DATABASE_RETRY_INTERVAL_SECONDS
from wt_media_agent.storage.dialect import SQLiteDialect

DATABASE_TYPE = 'sqlite'
DIALECTS = {'sqlite': SQLiteDialect}


class Database:
    def __init__(self, path: str | Path | None = None, *, data_dir: Path | None = None,
                 synchronous: str = DEFAULT_DATABASE_SYNCHRONOUS,
                 timeout_seconds: float = DEFAULT_DATABASE_TIMEOUT_SECONDS):
        try:
            dialect = DIALECTS[DATABASE_TYPE]
        except KeyError:
            raise ValueError(f'unsupported database type: {DATABASE_TYPE}') from None
        if (path is None) == (data_dir is None):
            raise ValueError('provide exactly one database path or data directory')
        location = Path(path) if path is not None else Path(data_dir) / DEFAULT_DB_NAME
        self.dialect = dialect(location, synchronous=synchronous, timeout_seconds=timeout_seconds)
        self.timeout_seconds = timeout_seconds
        self.engine = create_engine(self.dialect.url, **self.dialect.engine_options)
        self.dialect.configure(self.engine)
        self.session = sessionmaker(self.engine, expire_on_commit=False)

    @property
    def path(self):
        return self.dialect.path

    @property
    def migrations(self):
        return self.dialect.migrations

    def probe(self, table):
        self.dialect.check_available()
        with self.session() as session:
            session.execute(select(table).limit(1)).first()

    def insert_if_absent(self, session, model, values):
        return self.dialect.insert_if_absent(session, model, values)

    def upsert(self, session, model, values, changes, *, unique=None):
        self.dialect.upsert(session, model, values, changes, unique=unique)

    def apply_migration(self, connection, item, **context):
        self.dialect.apply_migration(connection, item, **context)

    def close(self):
        self.engine.dispose()


def retry_write(operation):
    """Retry only a database transaction whose failed Session has rolled back."""
    @wraps(operation)
    def write(self, *args, **kwargs):
        deadline = time.monotonic() + self.database.timeout_seconds
        while True:
            try:
                return operation(self, *args, **kwargs)
            except OperationalError as error:
                if not self.database.dialect.is_retryable(error.orig) or time.monotonic() >= deadline:
                    raise
                time.sleep(min(DATABASE_RETRY_INTERVAL_SECONDS, max(0, deadline-time.monotonic())))
    return write
