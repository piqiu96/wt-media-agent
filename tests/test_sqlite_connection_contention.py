"""Actual rollback-journal reader vs first WAL connection, as observed on CI."""
import concurrent.futures
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from wt_media_agent.storage import sqlite as policy


class SQLiteContentionTests(unittest.TestCase):
    def test_first_wal_open_waits_for_a_competing_connection_without_losing_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'fresh.db'
            original=sqlite3.connect
            holder=original(path)
            self.addCleanup(holder.close)
            holder.execute('CREATE TABLE facts(id INTEGER)')
            holder.commit()
            holder.execute('BEGIN')
            holder.execute('SELECT * FROM facts').fetchall()
            busy_seen=threading.Event()
            class ObservedConnection(sqlite3.Connection):
                def execute(self, sql, parameters=()):
                    try:
                        return super().execute(sql,parameters)
                    except sqlite3.OperationalError as exc:
                        if getattr(exc,'sqlite_errorcode',0)&255 == sqlite3.SQLITE_BUSY:
                            busy_seen.set()
                        raise
            def open_observed(*args,**kwargs):
                return original(*args,**kwargs,timeout=0,factory=ObservedConnection)
            with mock.patch.object(policy.sqlite3,'connect',side_effect=open_observed):
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    def open_and_check():
                        connection=policy.connect(path)
                        try:
                            return connection.row_factory, connection.execute('PRAGMA journal_mode').fetchone()[0]
                        finally:
                            connection.close()
                    future=pool.submit(open_and_check)
                    self.assertTrue(busy_seen.wait(5),'test must encounter real SQLITE_BUSY, not a mocked success')
                    holder.commit()
                    row_factory,mode=future.result(timeout=5)
                    self.assertIs(row_factory,sqlite3.Row)
                    self.assertEqual(mode,'wal')

    def test_non_contention_setup_failure_closes_the_connection(self):
        connection=sqlite3.connect(':memory:')
        connection.set_authorizer(lambda action,*args: sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_PRAGMA else sqlite3.SQLITE_OK)
        with mock.patch.object(policy.sqlite3,'connect',return_value=connection):
            with self.assertRaises(sqlite3.DatabaseError): policy.connect('ignored')
        with self.assertRaises(sqlite3.ProgrammingError): connection.execute('SELECT 1')

    def test_non_contention_open_failure_is_not_retried(self):
        with mock.patch.object(policy.sqlite3,'connect',side_effect=sqlite3.OperationalError('not usable')) as opening:
            with self.assertRaises(sqlite3.OperationalError): policy.connect('/invalid-test-path')
            opening.assert_called_once()

if __name__=='__main__': unittest.main()
