"""Actual SQLite and restart boundaries; no real platform success is simulated."""
import concurrent.futures
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from wt_media_agent.storage.migration import apply_migrations
from wt_media_agent.storage.publication import PublicationJournal, ExecutionIdentity, JournalConflict
from wt_media_agent.runner.publication import PublicationExecution, SubmissionBlocked


def identity(**changes):
    return ExecutionIdentity(**dict(environment='http://cloud.test:8188', user_id='3', task_id='9', attempt_id='1', bit_profile_id='actual-bit-profile', **changes))


def snapshot():
    return {'platform': 'baidu', 'platform_uid': '6572476037', 'video_sha256': 'a'*64,
            'video_size': 16, 'cover_sha256': 'b'*64, 'cover_size': 8,
            'content': {'description': '真实输入，不发起公开投稿', 'topics': '', 'declaration': '无需声明'}}


class PublicationExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name)/'agent.sqlite3'
        apply_migrations(self.db)
        self.journal = PublicationJournal(self.db)
        self.owner = identity()

    def run_execution(self, *, journal=None, preflight=None, submit=None, result=None):
        events = []
        journal = journal or self.journal
        def before_click(seq):
            reopened = PublicationJournal(self.db).get(self.owner)
            self.assertEqual(reopened['phase'], 'submitting')
            self.assertEqual(reopened['report_seq'], seq)
            events.append('preflight')
            return True if preflight is None else preflight(seq)
        def click():
            self.assertEqual(PublicationJournal(self.db).get(self.owner)['phase'], 'click_started')
            events.append('click')
            if submit: submit()
        outcome = PublicationExecution(journal).execute(
            self.owner, snapshot(), prepare=lambda: events.append('prepare'),
            preflight=before_click, submit=click,
            read_result=lambda: result or {'status': 'uncertain', 'evidence': {'reason': 'test fixture, not platform evidence'}},
        )
        return outcome, events

    def test_first_execution_persists_before_click_and_before_any_report(self):
        value, events = self.run_execution()
        self.assertEqual(events, ['prepare', 'preflight', 'click'])
        self.assertEqual(value['status'], 'uncertain')
        row = PublicationJournal(self.db).get(self.owner)
        self.assertEqual(row['phase'], 'result_saved')
        pending = self.journal.pending(self.owner.environment, self.owner.user_id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['packet']['result'], value)
        self.assertEqual(pending[0]['packet']['report_seq'], row['report_seq'])

    def test_same_claim_replay_after_restart_never_calls_browser(self):
        self.run_execution()
        outcome, events = self.run_execution(journal=PublicationJournal(self.db))
        self.assertEqual(events, [])
        self.assertEqual(outcome['status'], 'uncertain')
        self.assertEqual(len(self.journal.pending(self.owner.environment, '3')), 1)

    def test_lost_permission_response_and_explicit_refusal_do_not_click(self):
        for action in (lambda _: False, lambda _: (_ for _ in ()).throw(ConnectionError('lost response'))):
            with self.subTest(action=action):
                with tempfile.TemporaryDirectory() as tmp:
                    db=Path(tmp)/'agent.db'; apply_migrations(db); journal=PublicationJournal(db)
                    calls=[]
                    with self.assertRaises((SubmissionBlocked, ConnectionError)):
                        PublicationExecution(journal).execute(self.owner, snapshot(), prepare=lambda: None,
                            preflight=action, submit=lambda: calls.append('click'), read_result=lambda: {})
                    self.assertEqual(calls, [])
                    self.assertEqual(journal.get(self.owner)['phase'], 'submitting')
                    PublicationExecution(PublicationJournal(db)).execute(self.owner, snapshot(), prepare=lambda: self.fail('restarted prepare'), preflight=action, submit=lambda: self.fail('restarted click'), read_result=lambda: {})

    def test_crash_during_click_never_replays_click(self):
        with self.assertRaises(SystemExit):
            self.run_execution(submit=lambda: (_ for _ in ()).throw(SystemExit(77)))
        self.assertEqual(self.journal.get(self.owner)['phase'], 'click_started')
        outcome, events = self.run_execution(journal=PublicationJournal(self.db))
        self.assertEqual(events, [])
        self.assertEqual(outcome['status'], 'uncertain')
        self.assertEqual(self.journal.pending(self.owner.environment, '3'), [])

    def test_same_profile_is_reserved_across_owners_and_restarts(self):
        self.journal.register(self.owner, snapshot())
        other = ExecutionIdentity(self.owner.environment, '4', '10', '2', self.owner.bit_profile_id)
        with self.assertRaises(JournalConflict): PublicationJournal(self.db).register(other, snapshot())
        self.run_execution()
        with self.assertRaises(JournalConflict): self.journal.register(other, snapshot())

    def test_concurrent_claim_same_attempt_is_inserted_once(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            values=list(pool.map(lambda _: PublicationJournal(self.db).register(self.owner, snapshot()), range(8)))
        self.assertEqual(values.count(True), 1)
        self.assertEqual(values.count(False), 7)

    def test_same_attempt_different_input_or_task_cannot_overwrite(self):
        self.journal.register(self.owner, snapshot())
        changed=snapshot(); changed['content']['description']='changed'
        with self.assertRaises(JournalConflict): self.journal.register(self.owner, changed)
        altered=ExecutionIdentity(self.owner.environment, '3', '10', '1', self.owner.bit_profile_id)
        with self.assertRaises(JournalConflict): self.journal.register(altered, snapshot())
        self.assertEqual(self.journal.get(self.owner)['input'], snapshot())

    def test_environment_user_attempt_and_download_namespaces_do_not_collide(self):
        self.journal.register(self.owner, snapshot())
        identities=[ExecutionIdentity('http://other.test', '3', '9', '1', 'other-environment-profile'),
                    ExecutionIdentity(self.owner.environment, '4', '9', '1', 'other-profile')]
        for item in identities: self.assertTrue(self.journal.register(item, snapshot()))
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO task_checkpoints(task_id,task_type,agent_id,checkpoint_status,created_at,updated_at) VALUES('9','download','agent','transferring','now','now')")
            self.assertEqual(db.execute('SELECT COUNT(*) FROM task_checkpoints').fetchone()[0], 4)

    def test_result_is_atomic_idempotent_and_immutable(self):
        self.journal.register(self.owner, snapshot())
        first=self.journal.save_result(self.owner, {'status':'uncertain'}, report_seq=1)
        self.assertEqual(self.journal.save_result(self.owner, {'status':'uncertain'}, report_seq=1), first)
        for seq, result in [(0,{'status':'uncertain'}), (1,{'status':'failed'}), (2,{'status':'failed'})]:
            with self.assertRaises(JournalConflict): self.journal.save_result(self.owner, result, report_seq=seq)
        self.assertEqual(len(self.journal.pending(self.owner.environment,'3')),1)
        # A real SQLite trigger injects failure after checkpoint update: both writes roll back.
        another=ExecutionIdentity(self.owner.environment,'3','10','2','profile-2')
        self.journal.register(another,snapshot())
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TRIGGER reject_outbox BEFORE INSERT ON offline_results BEGIN SELECT RAISE(ABORT,'disk write failure fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.journal.save_result(another,{'status':'uncertain'},report_seq=1)
        self.assertEqual(self.journal.get(another)['phase'],'claimed')
        self.assertEqual(len(self.journal.pending(self.owner.environment,'3')),1)

    def test_outbox_replays_exact_result_only_for_current_owner_and_environment(self):
        self.run_execution()
        original=self.journal.pending(self.owner.environment,'3')[0]
        sent=[]
        def unavailable(packet): sent.append(packet); raise ConnectionError('offline')
        self.assertEqual(self.journal.flush(self.owner.environment,'3',unavailable),0)
        self.assertEqual(self.journal.flush(self.owner.environment,'4',lambda _:self.fail('foreign user')),0)
        self.assertEqual(self.journal.flush('http://other.test','3',lambda _:self.fail('foreign environment')),0)
        def accepted(packet):
            sent.append(packet)
            return {'attempt_id': self.owner.attempt_id, 'result_hash':packet['result_hash']}
        self.assertEqual(PublicationJournal(self.db).flush(self.owner.environment,'3',accepted),1)
        self.assertEqual(sent,[original['packet'],original['packet']])
        self.assertEqual(self.journal.pending(self.owner.environment,'3'),[])
        # Delivery ack is not proof the unknown browser scene is safe or a licence to rerun.
        _,events=self.run_execution(); self.assertEqual(events,[])
        other=ExecutionIdentity(self.owner.environment,'3','10','2',self.owner.bit_profile_id)
        with self.assertRaises(JournalConflict): self.journal.register(other,snapshot())

    def test_bad_ack_or_terminal_hash_keeps_result_pending(self):
        self.run_execution()
        for ack in ({}, {'attempt_id':'999','result_hash':'x'}, {'attempt_id':'1','result_hash':'x'}):
            self.assertEqual(self.journal.flush(self.owner.environment,'3',lambda _,a=ack:a),0)
        self.assertEqual(len(self.journal.pending(self.owner.environment,'3')),1)
        with self.assertRaises(JournalConflict): self.journal.settle(self.owner,'x',task_status='published')

    def test_only_confirmed_resolution_can_release_unknown_profile(self):
        self.run_execution()
        pending=self.journal.pending(self.owner.environment,'3')[0]
        with self.assertRaises(JournalConflict): self.journal.settle(self.owner,pending['packet']['result_hash'],task_status='uncertain')
        self.journal.flush(self.owner.environment,'3',lambda packet:{'attempt_id':'1','result_hash':packet['result_hash']})
        self.journal.settle(self.owner,pending['packet']['result_hash'],task_status='published')
        other=ExecutionIdentity(self.owner.environment,'3','10','2',self.owner.bit_profile_id)
        self.assertTrue(self.journal.register(other,snapshot()))
        _,events=self.run_execution(); self.assertEqual(events,[])

    def test_same_cloud_url_aliases_cannot_bypass_the_replay_barrier(self):
        self.journal.register(self.owner,snapshot())
        alias=ExecutionIdentity('HTTP://CLOUD.TEST:8188/','3','9','1',self.owner.bit_profile_id)
        self.assertFalse(self.journal.register(alias,snapshot()))
        self.journal.save_result(self.owner,{'status':'uncertain'},report_seq=1)
        self.assertEqual(len(self.journal.pending('HTTP://CLOUD.TEST:8188/','3')),1)
        self.assertEqual(ExecutionIdentity('https://cloud.test:443/','3','9','1','p').environment,'https://cloud.test')

    def test_explicit_scene_resolution_preserves_missing_result_and_replay_barrier(self):
        self.journal.register(self.owner, snapshot())
        resolution = {'attempt_id': self.owner.attempt_id, 'result_hash': None,
                      'scene_resolved': True, 'resolved_status': 'failed'}
        self.journal.settle_resolved(self.owner, resolution)
        reopened = PublicationJournal(self.db)
        self.assertEqual(reopened.get(self.owner)['phase'], 'settled')
        self.assertEqual(reopened.get(self.owner)['scene_resolution'], resolution)
        self.assertFalse(reopened.register(self.owner, snapshot()))
        with self.assertRaises(JournalConflict):
            reopened.save_result(self.owner, {'status': 'uncertain'}, report_seq=1)
        self.assertEqual(reopened.pending(self.owner.environment, self.owner.user_id), [])
        with sqlite3.connect(self.db) as db:
            self.assertIsNone(db.execute('SELECT final_result_json FROM task_checkpoints WHERE task_id=?', (self.owner.key,)).fetchone()[0])

    def test_scene_resolution_cannot_hide_a_different_original_result(self):
        self.journal.register(self.owner, snapshot())
        self.journal.save_result(self.owner, {'status': 'uncertain'}, report_seq=1)
        resolution = {'attempt_id': self.owner.attempt_id, 'result_hash': 'c' * 64,
                      'scene_resolved': True, 'resolved_status': 'failed'}
        with self.assertRaises(JournalConflict):
            self.journal.settle_resolved(self.owner, resolution)
        self.assertEqual(self.journal.get(self.owner)['phase'], 'result_saved')
        self.assertEqual(len(self.journal.pending(self.owner.environment, self.owner.user_id)), 1)

    def test_profile_reservation_is_physical_even_across_cloud_environments(self):
        self.journal.register(self.owner, snapshot())
        other=ExecutionIdentity('http://other.test','4','20','9',self.owner.bit_profile_id)
        with self.assertRaises(JournalConflict): self.journal.register(other,snapshot())

    def test_abrupt_process_exit_at_each_submit_boundary_never_replays(self):
        for boundary in ('submitting', 'click_started', 'result_saved'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                db=Path(tmp)/'restart.sqlite3'; click_file=Path(tmp)/'actual-click-count'
                apply_migrations(db)
                code = """
import json, os, sys
from pathlib import Path
from wt_media_agent.storage.publication import PublicationJournal, ExecutionIdentity
journal=PublicationJournal(sys.argv[1]); identity=ExecutionIdentity(**json.loads(sys.argv[3]))
journal.register(identity,json.loads(sys.argv[4]))
journal.advance(identity,'prepared'); journal.advance(identity,'submitting')
if sys.argv[2]=='submitting': os._exit(77)
journal.advance(identity,'click_started')
with open(sys.argv[5],'w') as marker:
    marker.write('one'); marker.flush(); os.fsync(marker.fileno())
if sys.argv[2]=='click_started': os._exit(77)
journal.save_result(identity,{'status':'uncertain','evidence':{'reason':'process fixture'}},report_seq=4)
os._exit(77)
"""
                from dataclasses import asdict
                child=subprocess.run([sys.executable,'-c',code,str(db),boundary,json.dumps(asdict(self.owner)),json.dumps(snapshot()),str(click_file)],capture_output=True,text=True)
                self.assertEqual(child.returncode,77,child.stderr)
                journal=PublicationJournal(db)
                self.assertEqual(journal.get(self.owner)['phase'],boundary)
                callbacks=[]
                PublicationExecution(journal).execute(self.owner,snapshot(),prepare=lambda:callbacks.append('prepare'),preflight=lambda _:True,submit=lambda:callbacks.append('click'),read_result=lambda:{})
                self.assertEqual(callbacks,[])
                self.assertEqual(click_file.read_text() if click_file.exists() else '', '' if boundary=='submitting' else 'one')
                self.assertEqual(len(journal.pending(self.owner.environment,'3')),1 if boundary=='result_saved' else 0)

    def test_secrets_and_bad_identities_are_rejected_before_persist(self):
        for change in ({'download_url':'https://secret.test/?signature=secret'}, {'content':{'cookie':'BDUSS=secret'}}, {'video_size':0}):
            data=snapshot(); data.update(change)
            with self.assertRaises(ValueError): self.journal.register(self.owner,data)
        for env in ['http://user:password@cloud.test','http://cloud.test/?token=secret','file:///tmp/x']:
            with self.assertRaises(ValueError): ExecutionIdentity(env,'3','9','1','profile')
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM task_checkpoints').fetchone()[0],0)

if __name__=='__main__': unittest.main()
