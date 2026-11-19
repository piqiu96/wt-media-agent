"""Real first-WAL contention remains bounded after the ORM transition."""
import concurrent.futures
import sqlite3
import sqlite3.dbapi2 as dbapi
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock
from sqlalchemy.exc import DatabaseError, OperationalError
from wt_media_agent.storage.database import Database

class SQLiteContentionTests(unittest.TestCase):
    def test_first_wal_open_waits_for_a_competing_connection_without_losing_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'fresh.db'
            original=sqlite3.connect
            with closing(original(path)) as holder:
                holder.execute('CREATE TABLE facts(id INTEGER)');holder.commit()
                holder.execute('BEGIN');holder.execute('SELECT * FROM facts').fetchall()
                seen=threading.Event()
                class Cursor(sqlite3.Cursor):
                    def execute(self, sql, parameters=()):
                        try: return super().execute(sql,parameters)
                        except sqlite3.OperationalError as exc:
                            if getattr(exc,'sqlite_errorcode',0)&255==sqlite3.SQLITE_BUSY: seen.set()
                            raise
                class Connection(sqlite3.Connection):
                    def cursor(self): return super().cursor(factory=Cursor)
                def opening(*args,**kwargs):
                    kwargs.update(timeout=0,factory=Connection)
                    return original(*args,**kwargs)
                database=Database(path)
                self.addCleanup(database.close)
                with mock.patch.object(dbapi,'connect',side_effect=opening):
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        def check():
                            with database.engine.connect() as conn:
                                return tuple(conn.exec_driver_sql('PRAGMA '+name).scalar() for name in ('journal_mode','foreign_keys','synchronous'))
                        future=pool.submit(check)
                        self.assertTrue(seen.wait(5),'must encounter real SQLITE_BUSY')
                        holder.commit()
                        self.assertEqual(future.result(timeout=5),('wal',1,2))

    def test_non_contention_setup_failure_closes_the_connection(self):
        connection=sqlite3.connect(':memory:',autocommit=False)
        connection.set_authorizer(lambda action,*args: sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_PRAGMA else sqlite3.SQLITE_OK)
        database=Database(':memory:')
        self.addCleanup(database.close)
        with mock.patch.object(dbapi,'connect',return_value=connection):
            with self.assertRaises(DatabaseError): database.engine.connect()
        with self.assertRaises(sqlite3.ProgrammingError): connection.execute('SELECT 1')

    def test_non_contention_open_failure_is_not_retried(self):
        database=Database('/private/tmp/invalid-chg102-test/agent.db')
        self.addCleanup(database.close)
        with mock.patch.object(dbapi,'connect',side_effect=sqlite3.OperationalError('not usable')) as opening:
            with self.assertRaises(OperationalError): database.engine.connect()
            opening.assert_called_once()
