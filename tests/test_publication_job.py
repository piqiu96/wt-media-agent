from wt_media_agent.storage.database import Database
"""Coordinator fault checks use real SQLite; fixture receipts are not platform acceptance."""
import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from unittest.mock import patch
from wt_media_agent.storage.migration import apply_migrations
from wt_media_agent.storage.publication import ExecutionIdentity, PublicationJournal, JournalConflict
from test_publication_execution import identity, snapshot
from wt_media_agent.runner.publication_job import PublicationJob
from wt_media_agent.platforms.baidu.publisher import BaiduPublisher


class Cloud:
    def __init__(self, journal, ident):
        self.journal=journal; self.ident=ident; self.base_url=ident.cloud_env; self.reports=[]; self.permission=True; self.lose_permission=False; self.lose_result=False; self.resolved=False; self.stored_hash=None
    def report_publication(self, token, task, operation, packet):
        self.reports.append((operation,copy.deepcopy(packet)))
        if operation=='progress' and packet['stage']=='submitting':
            assert self.journal.get(self.ident)['phase']=='submitting'
            if self.lose_permission: raise ConnectionError('lost permission response')
            return {'stop_requested':False,'allow_submit':self.permission}
        if operation=='complete':
            if self.lose_result: raise ConnectionError('lost result response')
            self.stored_hash=packet['result_hash']
            return {'attempt_id':packet['attempt_id'],'result_hash':packet['result_hash'],'task_status':packet['result']['status']}
        return {'stop_requested':False}


    def original_publication_attempt(self,token,task,attempt):
        state=self.journal.get(self.ident)
        return {'attempt_id':attempt,'result_hash':self.stored_hash,'scene_resolved':self.resolved,'resolved_status':'failed' if self.resolved else ''}


class Preparation:
    def __init__(self,journal,cloud,ident,root):
        self.journal=journal;self.cloud_env=ident.cloud_env;self.cloud=cloud;self.report_lock=RLock();self.ident=ident;self.root=root;self.fresh=True
    def prepare(self,token,value,ident):
        return SimpleNamespace(identity=ident,video=self.root/'video',cover=self.root/'cover',endpoint='127.0.0.1:9999')
    def mark_ready(self, token, ident):
        self.journal.advance(ident, 'prepared')
    def consume_prepared(self,prepared):
        if not self.fresh:raise JournalConflict('no fresh preparation')
        self.fresh=False


class Platform:
    def __init__(self,journal,ident): self.journal=journal;self.ident=ident;self.fills=0;self.clicks=0;self.fail_click=False
    def fill_form(self,page,payload,*,stopped):
        self.fills+=1
        assert not stopped()
    def submit(self):
        assert self.journal.get(self.ident)['phase']=='click_started'
        self.clicks+=1
        if self.fail_click:raise ConnectionError('lost click acknowledgement')
    def read_receipt(self):return {'platform_work_id':'1834567890123456789'}


class Session:
    def __init__(self, endpoint): self.page=object(); self.disconnected=False
    def __enter__(self): return self
    def __exit__(self, *_): self.disconnected=True


class PublicationJobTests(unittest.TestCase):
    def setUp(self):
        self.live_identity=patch('wt_media_agent.platforms.baidu.publisher.read_baidu_identity').start()
        self.addCleanup(patch.stopall)
        self.live_identity.return_value={'login_status':'normal','platform_account_id':snapshot()['platform_uid']}
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name)
        db=root/'a.db';apply_migrations(db);self.j=PublicationJournal(Database(db));self.i=identity();self.j.register(self.i,snapshot())
        self.cloud=Cloud(self.j,self.i);self.prep=Preparation(self.j,self.cloud,self.i,root);self.platform=Platform(self.j,self.i);self.closed=True
        self.bit=SimpleNamespace(close_profile_verified=lambda *a,**kw:self.closed)
        self.session=Session("127.0.0.1:9999")
        self.job=PublicationJob(self.prep,self.bit,lambda platform: lambda endpoint: BaiduPublisher(
            endpoint, browser_session_factory=lambda endpoint:self.session, browser_factory=lambda:self.platform),
            heartbeat_interval=0.02)
        s=snapshot();s.pop('platform')
        self.value={'task':{'id':self.i.task_id,'platform':'baidu'},'attempt':{'input_snapshot':dict(s,bit_profile_id=self.i.bit_profile_id)}}
    def run_job(self):return self.job.run('memory-only-token',self.value,self.i)
    def test_settled_scene_keeps_original_result_visible_after_restart_and_network_restore(self):
        self.closed=False
        self.cloud.lose_result=True
        self.run_job()
        self.cloud.resolved=True
        self.job.recover('token','3')
        self.cloud.lose_result=False
        original=self.cloud.report_publication
        def reject_late_result(token,task,operation,packet):
            if operation=='complete':
                self.cloud.reports.append((operation,copy.deepcopy(packet)))
                raise ValueError('Cloud rejects a result after manual resolution')
            return original(token,task,operation,packet)
        self.cloud.report_publication=reject_late_result
        before=sum(operation=='complete' for operation,_ in self.cloud.reports)
        self.job.journal=PublicationJournal(Database(self.j.database.path))
        self.job.recover('token','3')
        after=sum(operation=='complete' for operation,_ in self.cloud.reports)
        self.assertEqual(after-before,1)
        self.assertEqual(self.platform.clicks,1)
        self.assertEqual(len(self.job.journal.pending(self.i.cloud_env,'3')),1)
        self.assertEqual(self.job.journal.get(self.i)['phase'],'settled')

    def test_click_requires_both_durable_barriers_and_only_receipt_means_reviewing(self):
        result=self.run_job()
        self.assertEqual(result['status'],'reviewing');self.assertEqual(self.platform.clicks,1)
        self.assertEqual(self.j.get(self.i)['phase'],'settled');self.assertEqual(self.j.pending(self.i.cloud_env,self.i.user_id),[])
        with self.assertRaises(JournalConflict):self.run_job()
        self.assertEqual(self.platform.clicks,1)
    def test_lost_permission_never_clicks_and_restart_cannot_reenter(self):
        self.cloud.lose_permission=True
        result=self.run_job()
        self.assertEqual(result['status'],'uncertain');self.assertEqual(self.platform.clicks,0)
        self.assertEqual(self.j.get(self.i)['phase'],'result_saved')
        with self.assertRaises(JournalConflict):self.run_job()
        self.assertEqual(self.platform.fills,1)
    def test_lost_click_response_is_unknown_and_never_retried(self):
        self.platform.fail_click=True
        self.assertEqual(self.run_job()['status'],'uncertain');self.assertEqual(self.platform.clicks,1)
        with self.assertRaises(JournalConflict):self.run_job()
        self.assertEqual(self.platform.clicks,1)
    def test_result_saved_before_network_and_recovery_resends_original_only(self):
        self.cloud.lose_result=True
        result=self.run_job();self.assertEqual(result['status'],'reviewing')
        packet=self.j.pending(self.i.cloud_env,self.i.user_id)[0]['packet']
        self.cloud.lose_result=False
        self.assertEqual(self.job.recover('memory-only-token',self.i.user_id),1)
        self.assertEqual(self.j.get(self.i)['phase'],'settled')
        sent=[p for op,p in self.cloud.reports if op=='complete']
        self.assertTrue(all(p=={k:v for k,v in packet.items() if k!='task_id'} for p in sent));self.assertEqual(self.platform.clicks,1)
    def test_unclosed_scene_is_uncertain_even_with_a_receipt(self):
        self.closed=False
        result=self.run_job()
        self.assertEqual(result['status'],'uncertain');self.assertEqual(self.j.get(self.i)['phase'],'result_saved')
        self.assertFalse(result['evidence']['scene_closed']);self.assertEqual(self.platform.clicks,1)
        self.assertEqual(result['platform_work_id'],'1834567890123456789')

    def test_explicit_resolution_releases_original_scene_even_after_task_advanced(self):
        self.closed=False
        result=self.run_job();self.assertEqual(result['status'],'uncertain')
        self.assertEqual(self.j.get(self.i)['phase'],'result_saved')
        self.cloud.lose_result=True;self.cloud.resolved=True
        self.assertEqual(self.job.recover('memory-only-token',self.i.user_id),1)
        self.assertEqual(self.j.get(self.i)['phase'],'settled')
        self.assertEqual(self.platform.clicks,1)

    def test_an_acknowledged_but_unsettled_crash_recovers_without_browser_work(self):
        original=self.j.settle
        def crash(*args,**kwargs):raise RuntimeError('process stopped after ack')
        self.j.settle=crash
        self.run_job()
        self.assertEqual(self.j.get(self.i)['phase'],'result_saved')
        self.assertEqual(self.j.pending(self.i.cloud_env,self.i.user_id),[])
        self.j.settle=original
        self.assertEqual(self.job.recover('memory-only-token',self.i.user_id),1)
        self.assertEqual(self.j.get(self.i)['phase'],'settled');self.assertEqual(self.platform.clicks,1)

    def test_hard_exit_without_a_result_waits_for_original_scene_resolution(self):
        self.j.advance(self.i, 'prepared')
        self.j.advance(self.i, 'submitting')
        self.j.advance(self.i, 'click_started')
        self.j = PublicationJournal(Database(self.j.database.path))
        self.job.journal = self.j
        before = self.j.get(self.i)
        self.assertEqual(self.job.recover('memory-only-token', self.i.user_id), 0)
        self.assertEqual(self.j.get(self.i), before)

        self.cloud.resolved = True
        self.assertEqual(self.job.recover('memory-only-token', self.i.user_id), 1)
        state = self.j.get(self.i)
        self.assertEqual(state['phase'], 'settled')
        self.assertEqual(state['report_seq'], before['report_seq'])
        self.assertEqual(self.cloud.reports, [])  # No fabricated completion.
        self.assertEqual(self.j.pending(self.i.cloud_env, self.i.user_id), [])
        self.assertEqual(self.platform.fills, 0)
        self.assertEqual(self.platform.clicks, 0)
        with self.assertRaises(JournalConflict):
            self.run_job()
        self.assertFalse(self.j.register(self.i, snapshot()))
        self.assertEqual(self.job.recover('memory-only-token', self.i.user_id), 0)
        new = ExecutionIdentity(self.i.cloud_env, self.i.user_id, self.i.task_id, '2', self.i.bit_profile_id)
        self.assertTrue(self.j.register(new, snapshot()))

    def test_manual_resolution_before_report_releases_scene_without_faking_delivery(self):
        self.closed = False
        self.cloud.lose_result = True
        self.run_job()
        pending = self.j.pending(self.i.cloud_env, self.i.user_id)
        self.assertEqual(len(pending), 1)
        self.assertIsNone(self.cloud.stored_hash)
        self.cloud.resolved = True

        self.assertEqual(self.job.recover('memory-only-token', self.i.user_id), 1)
        self.assertEqual(self.j.get(self.i)['phase'], 'settled')
        self.assertEqual(self.j.pending(self.i.cloud_env, self.i.user_id), pending)
        with closing(sqlite3.connect(self.j.database.path)) as db:
            row = db.execute('SELECT r.result_json,c.report_pending FROM task_result r JOIN task_checkpoint c ON c.id=r.checkpoint_id WHERE c.task_id=?', (self.i.task_id,)).fetchone()
            self.assertEqual(json.loads(row[0]), pending[0]['packet'])
            self.assertEqual(row[1], 1)
            self.assertEqual(db.execute('SELECT r.delivered FROM task_result r JOIN task_checkpoint c ON c.id=r.checkpoint_id WHERE c.task_id=?', (self.i.task_id,)).fetchone()[0], 0)
        self.assertEqual(self.platform.clicks, 1)
        self.assertEqual(self.job.recover('memory-only-token', self.i.user_id), 0)
        new = ExecutionIdentity(self.i.cloud_env, self.i.user_id, self.i.task_id, '2', self.i.bit_profile_id)
        self.assertTrue(self.j.register(new, snapshot()))

    def test_missing_result_recovery_is_scoped_to_current_environment_and_owner(self):
        reads = []
        original = self.cloud.original_publication_attempt
        self.cloud.original_publication_attempt = lambda *args: (reads.append(args), original(*args))[1]
        self.cloud.resolved = True
        self.assertEqual(self.job.recover('memory-only-token', '4'), 0)
        self.prep.cloud_env = 'prod'
        self.assertEqual(self.job.recover('memory-only-token', self.i.user_id), 0)
        self.assertEqual(reads, [])
        self.prep.cloud_env = self.i.cloud_env
        self.assertEqual(self.job.recover('memory-only-token', self.i.user_id), 1)
        self.assertEqual(len(reads), 1)

    def test_conflicting_or_incomplete_resolution_cannot_release_missing_result(self):
        for change in ({'attempt_id': '99'}, {'scene_resolved': False},
                       {'scene_resolved': 'true'}, {'resolved_status': 'uncertain'},
                       {'resolved_status': 'reviewing'}, {'result_hash': 'invalid'}):
            with self.subTest(change=change):
                response = dict(attempt_id=self.i.attempt_id, result_hash=None,
                                scene_resolved=True, resolved_status='failed')
                response.update(change)
                self.cloud.original_publication_attempt = lambda *args: response
                self.assertEqual(self.job.recover('memory-only-token', self.i.user_id), 0)
                self.assertEqual(self.j.get(self.i)['phase'], 'claimed')
        self.cloud.original_publication_attempt = lambda *args: (_ for _ in ()).throw(ConnectionError('offline'))
        self.assertEqual(self.job.recover('memory-only-token', self.i.user_id), 0)
        self.assertEqual(self.j.get(self.i)['phase'], 'claimed')

    def test_conflicting_saved_result_cannot_be_released_by_manual_failure(self):
        self.cloud.lose_result = True
        self.run_job()  # A credible receipt conflicts with an unacknowledged failure.
        self.cloud.resolved = True
        self.assertEqual(self.job.recover('memory-only-token', self.i.user_id), 0)
        self.assertEqual(self.j.get(self.i)['phase'], 'result_saved')
        self.cloud.stored_hash = 'c' * 64
        self.assertEqual(self.job.recover('memory-only-token', self.i.user_id), 0)
        self.assertEqual(self.j.get(self.i)['phase'], 'result_saved')

    def test_zero_or_noncanonical_receipt_cannot_be_reported_as_reviewing(self):
        self.platform.read_receipt=lambda:{'platform_work_id':'0'}
        result=self.run_job()
        self.assertEqual(result['status'],'uncertain')
        self.assertNotIn('platform_work_id',result)
        self.assertEqual(self.j.get(self.i)['phase'],'result_saved')
        self.assertEqual(self.platform.clicks,1)

    def test_cloud_stop_before_upload_never_enters_platform(self):
        report=self.cloud.report_publication
        def stop(token,task,operation,packet):
            if operation=='progress' and packet['stage']=='ready':
                return {'stop_requested':True}
            return report(token,task,operation,packet)
        self.cloud.report_publication=stop
        result=self.run_job()
        self.assertEqual(result['status'],'uncertain')
        self.assertEqual(self.platform.fills,0)
        self.assertEqual(self.platform.clicks,0)

    def test_changed_session_before_preparation_never_opens_browser(self):
        result=self.job.run('memory-only-token',self.value,self.i,current=lambda:False)
        self.assertEqual(result['status'],'uncertain');self.assertEqual(self.platform.fills,0);self.assertEqual(self.platform.clicks,0)
        self.assertEqual(self.j.get(self.i)['phase'],'result_saved')

    def test_session_replaced_after_submit_permission_never_clicks(self):
        live=[True];report=self.cloud.report_publication
        def replace(token,task,operation,packet):
            value=report(token,task,operation,packet)
            if operation=='progress' and packet['stage']=='submitting':live[0]=False
            return value
        self.cloud.report_publication=replace
        result=self.job.run('memory-only-token',self.value,self.i,current=lambda:live[0])
        self.assertEqual(result['status'],'uncertain');self.assertEqual(self.platform.fills,1);self.assertEqual(self.platform.clicks,0)
        self.assertEqual(self.j.get(self.i)['phase'],'result_saved')

    def test_driver_disconnects_before_profile_close_even_on_lost_click(self):
        events=[]
        session=self.session
        class ObservedSession:
            page=session.page
            def __enter__(self): events.append('connect');return self
            def __exit__(self,*_): events.append('disconnect')
        self.session=ObservedSession()
        def close(*_): events.append('profile-close');return True
        self.bit.close_profile_verified=close
        self.platform.fail_click=True
        self.run_job()
        self.assertEqual(events,['connect','disconnect','profile-close'])
        self.assertEqual(self.platform.clicks,1)

    def test_platform_dispatch_rejects_before_opening_browser(self):
        def unsupported(platform): raise ValueError('unsupported')
        self.job.platform_factory=unsupported
        def forbidden(*_): self.fail('unsupported platform opened browser')
        self.prep.prepare=forbidden
        self.assertEqual(self.run_job()['status'],'uncertain')
        self.assertEqual(self.platform.fills,0)

    def test_changed_platform_account_after_upload_cannot_get_submit_permission(self):
        self.live_identity.side_effect=[{'login_status':'normal','platform_account_id':snapshot()['platform_uid']}, {'login_status':'normal','platform_account_id':'different-account'}]
        result=self.run_job()
        self.assertEqual(result['status'],'uncertain')
        self.assertEqual(self.platform.fills,1);self.assertEqual(self.platform.clicks,0)
        self.assertFalse(any(op=='progress' and packet['stage']=='submitting' for op,packet in self.cloud.reports))
        self.assertTrue(result['evidence']['scene_closed'])

    def test_unavailable_final_identity_check_cannot_submit(self):
        self.live_identity.side_effect=ConnectionError('identity response unavailable')
        self.assertEqual(self.run_job()['status'],'uncertain')
        self.assertEqual(self.platform.clicks,0)
        self.assertFalse(any(op=='progress' and packet['stage']=='submitting' for op,packet in self.cloud.reports))
