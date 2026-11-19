"""Real SQLite regression gates for the ORM and identity redesign."""
import concurrent.futures
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from wt_media_agent.runtime.config import load_config, ConfigError

class OrmStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'agent.sqlite3'

    def database(self):
        from wt_media_agent.storage.database import Database
        db = Database(self.path)
        self.addCleanup(db.close)
        from wt_media_agent.storage.migration import apply_migrations
        apply_migrations(db)
        return db

    def test_explicit_cloud_environment_is_independent_of_build_and_url(self):
        config = load_config(config_dir=Path(self.tmp.name), env={'WT_MEDIA_ENV':'development', 'WT_MEDIA_CLOUD_ENV':'prod', 'WT_MEDIA_CLOUD_BASE_URL':'https://real-cloud.example'})
        self.assertEqual(config.cloud_env, 'prod')
        with self.assertRaises(ConfigError):
            load_config(config_dir=Path(self.tmp.name), env={'WT_MEDIA_CLOUD_ENV':'https://cloud.example'})

    def test_every_connection_enables_durability_and_foreign_keys(self):
        db = self.database()
        with db.engine.connect() as first, db.engine.connect() as second:
            for connection in (first, second):
                self.assertEqual(connection.exec_driver_sql('PRAGMA synchronous').scalar(), 2)
                self.assertEqual(connection.exec_driver_sql('PRAGMA foreign_keys').scalar(), 1)
                self.assertEqual(connection.exec_driver_sql('PRAGMA journal_mode').scalar(), 'wal')
        with closing(sqlite3.connect(self.path)) as raw:
            tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({'task_checkpoint','task_result','agent_metadata','schema_migrations'} <= tables)
        self.assertFalse({'task_checkpoints','offline_results'} & tables)

    def test_scoped_download_lookup_and_delete_do_not_touch_same_id_in_other_type(self):
        from wt_media_agent.storage.checkpoint_store import CheckpointStore, TaskCheckpoint
        db = self.database()
        first = CheckpointStore(db, cloud_env='dev', user_id=lambda:'3')
        other = CheckpointStore(db, cloud_env='prod', user_id=lambda:'3')
        first.save_checkpoint(TaskCheckpoint('9','material_download_task','agent','transferring',progress=7))
        first.save_checkpoint(TaskCheckpoint('9','another_type','agent','running',progress=11))
        other.save_checkpoint(TaskCheckpoint('9','material_download_task','agent','transferring',progress=19))
        self.assertEqual(first.get_checkpoint('9').progress, 7)
        first.remove_checkpoint('9')
        self.assertIsNone(first.get_checkpoint('9'))
        self.assertEqual(first.get_checkpoint('9', task_type='another_type').progress, 11)
        self.assertEqual(other.get_checkpoint('9').progress, 19)

    def test_default_transactions_keep_one_profile_owner_across_cloud_environments(self):
        from wt_media_agent.storage.publication import PublicationJournal, ExecutionIdentity, JournalConflict
        db = self.database()
        from test_publication_execution import snapshot
        def register(index):
            identity = ExecutionIdentity('dev' if index % 2 else 'prod',str(index+1),'9',str(index+1),'same-profile')
            try:
                return PublicationJournal(db).register(identity,snapshot())
            except JournalConflict:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(register,range(8))),1)

    def test_result_foreign_key_and_immutable_result_survive_reopen(self):
        from wt_media_agent.storage.publication import PublicationJournal, ExecutionIdentity, JournalConflict
        from test_publication_execution import snapshot
        db = self.database()
        journal = PublicationJournal(db)
        owner = ExecutionIdentity('dev','3','9','1','profile')
        journal.register(owner,snapshot())
        packet = journal.save_result(owner,{'status':'uncertain'},report_seq=1)
        self.assertEqual(journal.save_result(owner,{'status':'uncertain'},report_seq=1),packet)
        with self.assertRaises(JournalConflict):
            journal.save_result(owner,{'status':'failed'},report_seq=2)
        with closing(sqlite3.connect(self.path)) as raw:
            row=raw.execute('SELECT c.task_id,c.task_type,r.checkpoint_id,c.id FROM task_result r JOIN task_checkpoint c ON c.id=r.checkpoint_id').fetchone()
        self.assertEqual(row[:2],('9','publication'))
        self.assertEqual(row[2],row[3])
        self.assertEqual(PublicationJournal(db).pending('dev','3')[0]['packet'],packet)

    def legacy_database(self, *, publication=False):
        from wt_media_agent.storage.migration import MIGRATIONS
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,name TEXT,applied_at TEXT DEFAULT CURRENT_TIMESTAMP)')
            for migration in MIGRATIONS[:4]:
                for statement in migration.statements: db.execute(statement)
                db.execute('INSERT INTO schema_migrations(version,name) VALUES(?,?)',(migration.version,migration.name))
            if publication:
                state={'identity':{'environment':'https://old.example','user_id':'3','task_id':'9','attempt_id':'1','bit_profile_id':'profile'},'input':{'old':'frozen'},'phase':'result_saved','report_seq':1,'result_hash':'kept-hash'}
                packet={'task_id':'9','attempt_id':'1','report_seq':1,'result_hash':'kept-hash','result':{'status':'uncertain'}}
                db.execute("INSERT INTO task_checkpoints(task_id,task_type,agent_id,checkpoint_status,checkpoint_json,task_payload_json,attempt_id,report_pending,created_at,updated_at) VALUES('publication:old','publication','3','publication_result',?,?,'1',1,'before','before')",(json.dumps(state),json.dumps(state['input'])))
                db.execute("INSERT INTO offline_results(task_id,agent_id,status,result_json,queued_at) VALUES('publication:old','3','uncertain',?,'before')",(json.dumps(packet),))
            else:
                db.execute("INSERT INTO task_checkpoints(task_id,task_type,agent_id,checkpoint_status,created_at,updated_at,transfer_state_json) VALUES('9','material_download_task','device','transferring','before','before','{\"file_name\":\"kept.mp4\",\"bytes_done\":7}')")

            db.commit()

    def test_unmapped_legacy_owner_blocks_upgrade_without_altering_old_data(self):
        from wt_media_agent.storage.migration import apply_migrations, LegacyIdentityRequired
        self.legacy_database()
        with self.assertRaises(LegacyIdentityRequired): apply_migrations(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT task_id FROM task_checkpoints').fetchone()[0],'9')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0],4)
        apply_migrations(self.path,legacy_scope=('test','3'))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT cloud_env,user_id,task_id FROM task_checkpoint').fetchone(),('test','3','9'))

    def test_mapped_publication_preserves_original_packet_hash_and_pending_delivery(self):
        from wt_media_agent.storage.migration import apply_migrations
        self.legacy_database(publication=True)
        apply_migrations(self.path,cloud_env_map={'https://old.example':'prod'})
        self.assertEqual(apply_migrations(self.path),[])
        with closing(sqlite3.connect(self.path)) as db:
            packet,delivered=db.execute('SELECT result_json,delivered FROM task_result').fetchone()
            self.assertEqual(json.loads(packet)['result_hash'],'kept-hash')
            self.assertEqual(delivered,0)
            self.assertEqual(db.execute('SELECT cloud_env,user_id,task_id FROM task_checkpoint').fetchone(),('prod','3','9'))

    def test_session_failure_rolls_back_and_result_cannot_reference_missing_checkpoint(self):
        from sqlalchemy.exc import IntegrityError
        from wt_media_agent.storage.models import AgentMetadata, TaskResultModel
        db=self.database()
        with self.assertRaises(RuntimeError):
            with db.session.begin() as session:
                session.add(AgentMetadata(key='rolled-back',value='x',updated_at='before'))
                session.flush()
                raise RuntimeError('injected failure')
        with db.session() as session:
            self.assertIsNone(session.get(AgentMetadata,'rolled-back'))
        with self.assertRaises(IntegrityError):
            with db.session.begin() as session:
                session.add(TaskResultModel(checkpoint_id=999,status='uncertain',queued_at='before'))

    def test_unknown_pending_claim_does_not_rotate_its_request_key_during_upgrade(self):
        import hashlib
        from wt_media_agent.storage.migration import apply_migrations, LegacyIdentityRequired
        self.legacy_database()
        old='publication.claim.'+hashlib.sha256(b'["https://old.example","3"]').hexdigest()
        with closing(sqlite3.connect(self.path)) as raw:
            raw.execute('INSERT INTO agent_metadata(key,value,updated_at) VALUES(?,?,?)',(old,'original-request','before'))
            raw.commit()
        with self.assertRaises(LegacyIdentityRequired):
            apply_migrations(self.path,legacy_scope=('prod','3'))
        with closing(sqlite3.connect(self.path)) as raw:
            self.assertEqual(raw.execute('SELECT value FROM agent_metadata WHERE key=?',(old,)).fetchone()[0],'original-request')
        apply_migrations(self.path,legacy_scope=('prod','3'),cloud_env_map={'https://old.example':'prod'})
        with closing(sqlite3.connect(self.path)) as raw:
            self.assertEqual(raw.execute("SELECT value FROM agent_metadata WHERE key='publication.claim.prod.3'").fetchone()[0],'original-request')

    def test_concurrent_metadata_commit_does_not_lose_observed_result(self):
        from unittest.mock import patch
        from wt_media_agent.storage.models import AgentMetadata
        from wt_media_agent.storage.publication import PublicationJournal, ExecutionIdentity
        from test_publication_execution import snapshot
        db=self.database()
        journal=PublicationJournal(db)
        owner=ExecutionIdentity('dev','3','9','1','profile')
        journal.register(owner,snapshot())
        original=journal._write
        committed=[]
        def interleave(session,row,state,**values):
            if not committed:
                with db.session.begin() as writer:
                    writer.add(AgentMetadata(key='unrelated-write',value='kept',updated_at='before'))
                committed.append(True)
            return original(session,row,state,**values)
        with patch.object(journal,'_write',side_effect=interleave):
            packet=journal.save_result(owner,{'status':'uncertain'},report_seq=1)
        self.assertEqual(len(committed),1)
        self.assertEqual(journal.pending('dev','3')[0]['packet'],packet)
        self.assertEqual(packet['report_seq'],1)
        self.assertEqual(journal.get(owner)['phase'],'result_saved')
