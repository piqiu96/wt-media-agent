from wt_media_agent.storage.database import Database
"""Coordinator fault checks use real SQLite; fixture receipts are not platform acceptance."""
import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from threading import Event, RLock, Thread
from types import SimpleNamespace
from unittest.mock import patch
from wt_media_agent.storage.migration import apply_migrations
from wt_media_agent.storage.database import Database
from wt_media_agent.storage.publication import ExecutionIdentity, PublicationJournal, JournalConflict
from test_publication_execution import identity, snapshot
from wt_media_agent.runner.publication_job import PublicationJob
from wt_media_agent.platforms.baidu.publisher import BaiduPublisher


class Cloud:
    def __init__(self, journal, ident):
        self.journal=journal; self.ident=ident; self.base_url=ident.cloud_env; self.reports=[]; self.permission=True; self.lose_permission=False; self.lose_result=False; self.resolved=False; self.stored_hash=None
    def report_publication(self, token, task, operation, packet):
        self.reports.append((operation,copy.deepcopy(packet)))
        if operation=='report' and packet.get('stage')=='submitting':
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
        return SimpleNamespace(identity=ident,video=self.root/'video',cover=self.root/'cover',portrait_cover=self.root/'portrait-cover',endpoint='127.0.0.1:9999')
    def mark_ready(self, token, ident):
        from wt_media_agent.runner.publication_preparation import PublicationPreparation
        return PublicationPreparation.mark_ready(self,token,ident)
    def consume_prepared(self,prepared):
        if not self.fresh:raise JournalConflict('no fresh preparation')
        self.fresh=False


class Platform:
    def __init__(self,journal,ident): self.journal=journal;self.ident=ident;self.fills=0;self.clicks=0;self.fail_click=False
    def protect_scene(self,page): pass
    def fill_form(self,page,payload,*,stopped,replace_matching_draft=False,wait_for_human=None):
        self.fills+=1
        self.replace_matching_draft=replace_matching_draft
        assert not stopped()
    def submit(self):
        assert self.journal.get(self.ident)['phase']=='click_started'
        self.clicks+=1
        if self.fail_click:raise ConnectionError('lost click acknowledgement')
    def read_receipt(self):return {'platform_work_id':'1834567890123456789'}


class Session:
    def __init__(self, endpoint): self.page=SimpleNamespace(context=object()); self.disconnected=False
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

    def test_human_scene_continues_same_stack_and_rechecks_cloud_without_refill(self):
        entered, shown, result = Event(), Event(), []
        original_fill = self.platform.fill_form
        def fill(page,payload,*,stopped,replace_matching_draft=False,wait_for_human=None):
            original_fill(page,payload,stopped=stopped,replace_matching_draft=replace_matching_draft)
            entered.set()
            wait_for_human(shown.set)
        self.platform.fill_form = fill
        calls=[]
        def check(token,task,attempt,revision,*,resume=False):
            calls.append(resume)
            return {'stop_requested':False,'allow_submit':False,'task_status':'running' if resume else 'awaiting_human','attempt_id':attempt}
        self.cloud.publication_human = check
        worker=Thread(target=lambda:result.append(self.run_job()),daemon=True)
        worker.start()
        self.assertTrue(entered.wait(2))
        # The operation must be registered before the UI can touch this scene.
        for _ in range(100):
            try:
                self.job.handle_human_action('memory-only-token',self.i.user_id,self.i.task_id,self.i.attempt_id,'5',resume=False)
                break
            except JournalConflict:
                Event().wait(.01)
        self.assertTrue(shown.wait(2))
        with self.assertRaises(JournalConflict):
            self.job.handle_human_action('other-token',self.i.user_id,self.i.task_id,self.i.attempt_id,'5',resume=True)
        self.job.handle_human_action('memory-only-token',self.i.user_id,self.i.task_id,self.i.attempt_id,'5',resume=True)
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]['status'],'reviewing')
        self.assertEqual((self.platform.fills,self.platform.clicks),(1,1))
        self.assertEqual(calls,[False,True])
        ack=self.job.handle_human_action('memory-only-token',self.i.user_id,self.i.task_id,self.i.attempt_id,'5',resume=True)
        self.assertEqual(ack['attempt_id'],self.i.attempt_id)
        self.assertEqual(calls,[False,True])
        self.assertEqual((self.platform.fills,self.platform.clicks),(1,1))
        with self.assertRaises(JournalConflict):
            self.job.handle_human_action('memory-only-token',self.i.user_id,self.i.task_id,self.i.attempt_id,'6',resume=True)

    def test_human_continuation_rechecks_changed_account_before_submit(self):
        original_fill=self.platform.fill_form
        def fill(page,payload,*,stopped,replace_matching_draft=False,wait_for_human=None):
            original_fill(page,payload,stopped=stopped)
            wait_for_human(lambda:None)
        self.platform.fill_form=fill
        original=self.job.cloud.report_publication
        def report(token,task,operation,packet):
            value=original(token,task,operation,packet)
            if packet.get('stage')=='awaiting_human':
                self.live_identity.return_value={'login_status':'normal','platform_account_id':'changed-account'}
            return value
        self.job.cloud.report_publication=report
        self.job.cloud.publication_human=lambda *args,**kw:{'stop_requested':False,'allow_submit':False,'task_status':'running','attempt_id':self.i.attempt_id}
        result=[]
        thread=Thread(target=lambda:result.append(self.run_job()),daemon=True);thread.start()
        for _ in range(200):
            try:
                self.job.handle_human_action('memory-only-token',self.i.user_id,self.i.task_id,self.i.attempt_id,'5',resume=True)
                break
            except JournalConflict: Event().wait(.01)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]['status'],'failed')
        self.assertEqual((self.platform.fills,self.platform.clicks),(1,0))

    def test_cache_cleanup_follows_ack_without_reposting_on_retry(self):
        from wt_media_agent.runner.publication_cache import PublicationCacheCleanup
        directory = self.prep.root / 'publications'
        folder = directory / self.i.key
        folder.mkdir(parents=True)
        (folder / 'video.mp4').write_bytes(b'isolated publishing cache')
        cleaner = PublicationCacheCleanup(self.j, self.cloud, directory, authorized=lambda token, user: True)
        self.job.cache_cleanup = cleaner
        self.cloud.lose_result = True
        self.assertEqual(self.run_job()['status'], 'reviewing')
        self.assertTrue(folder.exists())
        self.assertNotIn('cache_cleanup', self.j.get(self.i))
        self.cloud.lose_result = False
        with patch.object(cleaner, '_remove', side_effect=PermissionError):
            self.job.recover('memory-only-token', self.i.user_id)
        self.assertEqual(self.j.get(self.i)['cache_cleanup']['status'], 'failed')
        cleaner._last_sweep.clear()
        self.job.recover('memory-only-token', self.i.user_id)
        self.assertFalse(folder.exists())
        self.assertEqual(self.j.get(self.i)['cache_cleanup']['status'], 'complete')
        self.assertEqual(self.platform.clicks, 1)
        self.assertEqual(self.j.pending(self.i.cloud_env, self.i.user_id), [])

    def test_click_requires_both_durable_barriers_and_only_receipt_means_reviewing(self):
        result=self.run_job()
        self.assertEqual(result['status'],'reviewing');self.assertEqual(self.platform.clicks,1)
        self.assertEqual(self.j.get(self.i)['phase'],'settled');self.assertEqual(self.j.pending(self.i.cloud_env,self.i.user_id),[])
        with self.assertRaises(JournalConflict):self.run_job()
        self.assertEqual(self.platform.clicks,1)
    def test_explicit_stop_before_permission_reports_unsubmitted_only_after_close(self):
        original = self.cloud.report_publication
        def deny(token, task, operation, packet):
            if operation == 'report' and packet.get('stage') == 'submitting':
                return {'stop_requested': True, 'allow_submit': False}
            return original(token, task, operation, packet)
        self.cloud.report_publication = deny
        result = self.run_job()
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['evidence']['not_submitted'])
        self.assertTrue(result['evidence']['scene_closed'])
        self.assertEqual(self.platform.clicks, 0)
        with self.assertRaises(JournalConflict):
            self.run_job()

    def test_cancelled_task_acknowledges_original_failed_result_without_extra_lookup(self):
        original = self.cloud.report_publication
        def stop_and_ack(token, task, operation, packet):
            if operation == 'report' and packet.get('stage') == 'submitting':
                return {'stop_requested': True, 'allow_submit': False}
            reply = original(token, task, operation, packet)
            if operation == 'complete':
                reply['task_status'] = 'cancelled'
            return reply
        self.cloud.report_publication = stop_and_ack
        self.cloud.original_publication_attempt = lambda *args: (_ for _ in ()).throw(ConnectionError('offline'))
        result = self.run_job()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.j.get(self.i)['phase'], 'settled')
        self.assertEqual(self.platform.clicks, 0)

    def test_explicit_stop_without_safe_close_remains_unknown(self):
        original = self.cloud.report_publication
        def deny(token, task, operation, packet):
            if operation == 'report' and packet.get('stage') == 'submitting':
                return {'stop_requested': True, 'allow_submit': False}
            return original(token, task, operation, packet)
        self.cloud.report_publication = deny
        self.closed = False
        result = self.run_job()
        self.assertEqual(result['status'], 'uncertain')
        self.assertFalse(result['evidence']['scene_closed'])
        self.assertNotIn('not_submitted', result['evidence'])
        self.assertEqual(self.platform.clicks, 0)

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

    def test_public_resolution_settles_only_the_matching_original_packet(self):
        self.closed=False
        self.run_job()
        packet=self.j.unresolved_attempts(self.i.cloud_env,self.i.user_id)[0]['packet']
        self.cloud.original_publication_attempt=lambda token,task,attempt: {'attempt_id':attempt,'result_hash':packet['result_hash'],'scene_resolved':True,'resolved_status':'published'}
        self.assertEqual(self.job.recover('token',self.i.user_id),1)
        self.assertEqual(self.j.get(self.i)['phase'],'settled')
        self.assertEqual(self.platform.clicks,1)

    def test_public_resolution_closes_missing_result_without_inventing_packet(self):
        self.j.register(self.i,snapshot())
        self.cloud.original_publication_attempt=lambda token,task,attempt: {'attempt_id':attempt,'result_hash':None,'scene_resolved':True,'resolved_status':'published'}
        self.assertEqual(self.job.recover('token',self.i.user_id),1)
        self.assertEqual(self.j.get(self.i)['phase'],'settled')
        self.assertEqual(self.j.pending(self.i.cloud_env,self.i.user_id),[])
        self.assertEqual(self.platform.clicks,0)

    def test_reviewing_resolution_closes_missing_result_without_new_submission(self):
        self.j.register(self.i,snapshot())
        self.cloud.original_publication_attempt=lambda token,task,attempt: {'attempt_id':attempt,'result_hash':None,'scene_resolved':True,'resolved_status':'reviewing'}
        self.assertEqual(self.job.recover('token',self.i.user_id),1)
        self.assertEqual(self.j.get(self.i)['phase'],'settled')
        self.assertEqual(self.platform.clicks,0)

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
                       {'resolved_status': 'uncertain'}, {'result_hash': 'invalid'}):
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
            if operation=='report' and packet.get('stage')=='ready':
                return {'stop_requested':True}
            return report(token,task,operation,packet)
        self.cloud.report_publication=stop
        result=self.run_job()
        self.assertEqual(result['status'],'failed')
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
            if operation=='report' and packet.get('stage')=='submitting':live[0]=False
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
        self.assertEqual(result['status'],'failed')
        self.assertEqual(self.platform.fills,1);self.assertEqual(self.platform.clicks,0)
        self.assertFalse(any(op=='report' and packet.get('stage')=='submitting' for op,packet in self.cloud.reports))
        self.assertTrue(result['evidence']['scene_closed'])

    def test_unavailable_final_identity_check_cannot_submit(self):
        self.live_identity.side_effect=ConnectionError('identity response unavailable')
        self.assertEqual(self.run_job()['status'],'failed')
        self.assertEqual(self.platform.clicks,0)
        self.assertFalse(any(op=='report' and packet.get('stage')=='submitting' for op,packet in self.cloud.reports))


    def test_input_change_before_permission_fails_only_after_verified_close(self):
        from wt_media_agent.runner.publication_preparation import PublicationPreparation
        self.prep.mark_ready=lambda token,ident: PublicationPreparation.mark_ready(self.prep,token,ident)
        original=self.cloud.report_publication
        def stop(token,task,op,packet):
            if op=='report' and packet.get('stage')=='ready':
                return {'stop_requested':True,'stop_reason':'PUBLICATION_INPUT_CHANGED'}
            return original(token,task,op,packet)
        self.cloud.report_publication=stop
        result=self.run_job()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['error_code'],'PUBLICATION_INPUT_CHANGED')
        self.assertTrue(result['evidence']['scene_closed'])
        self.assertTrue(result['evidence']['not_submitted'])
        self.assertEqual(self.platform.clicks,0)

    def test_input_change_with_unclosed_scene_keeps_uncertain_and_occupied(self):
        original=self.cloud.report_publication
        def stop(token,task,op,packet):
            if op=='report' and packet.get('stage')=='ready':
                return {'stop_requested':True,'stop_reason':'PUBLICATION_INPUT_CHANGED'}
            return original(token,task,op,packet)
        self.cloud.report_publication=stop
        self.closed=False
        result=self.run_job()
        self.assertEqual(result['status'],'uncertain')
        self.assertTrue(self.j.profile_in_use(self.i.bit_profile_id))
        self.assertEqual(self.platform.clicks,0)

    def test_keepalive_and_stages_share_report_with_increasing_sequences(self):
        pulsed = Event()
        submitted_pulse = Event()
        report = self.cloud.report_publication
        def observed_report(token, task, operation, packet):
            value = report(token, task, operation, packet)
            if 'stage' not in packet and operation != 'complete':
                pulsed.set()
                if self.j.get(self.i)['phase'] == 'click_started':
                    submitted_pulse.set()
            return value
        self.cloud.report_publication = observed_report
        fill = self.platform.fill_form
        def wait_for_keepalive(page, payload, *, stopped, replace_matching_draft=False,wait_for_human=None):
            fill(page, payload, stopped=stopped)
            self.assertTrue(pulsed.wait(2), 'upload did not receive a keepalive')
        self.platform.fill_form = wait_for_keepalive
        receipt = self.platform.read_receipt
        def wait_for_receipt_keepalive():
            self.assertTrue(submitted_pulse.wait(2), 'submitted execution did not receive a keepalive')
            return receipt()
        self.platform.read_receipt = wait_for_receipt_keepalive
        result = self.run_job()
        self.assertEqual(result['status'], 'reviewing')
        self.assertEqual(self.platform.clicks, 1)
        self.assertTrue(pulsed.is_set())
        self.assertTrue(all(op in ('report', 'complete') for op, _ in self.cloud.reports))
        packets = [packet for op, packet in self.cloud.reports if op == 'report']
        alive = [packet for packet in packets if 'stage' not in packet]
        self.assertTrue(alive)
        self.assertTrue(all(set(packet) == {'attempt_id', 'report_seq'} for packet in alive))
        sequences = [packet['report_seq'] for packet in packets]
        self.assertTrue(all(a < b for a, b in zip(sequences, sequences[1:])))
        self.assertEqual([packet['stage'] for packet in packets if 'stage' in packet],
                         ['preparing', 'ready', 'submitting'])

    def test_failure_log_identifies_code_site_without_private_exception_text(self):
        def private_failure(*args, **kwargs):
            raise ValueError('Page.goto: net::ERR_ABORTED at https://private.example/?token=secret private publication text')
        self.platform.fill_form=private_failure
        with self.assertLogs('wt_media_agent.runner.publication_job',level='WARNING') as captured:
            self.run_job()
        output=' '.join(captured.output)
        self.assertIn('failure_site=test_publication_job.py:private_failure:',output)
        self.assertIn('browser_error=net::ERR_ABORTED',output)
        self.assertIn('failure_path=publication_job.py:run:',output)
        self.assertNotIn('private publication text',output)
        self.assertNotIn('private.example',output)
        self.assertNotIn('token=secret',output)
        self.assertEqual(self.platform.clicks,0)

    def test_only_new_same_input_attempt_gets_safe_draft_replacement_control(self):
        from dataclasses import replace
        fill=self.platform.fill_form
        def fail_before_submission(*args, **kwargs):
            raise ValueError('fixture failure before submission')
        self.platform.fill_form=fail_before_submission
        self.assertEqual(self.run_job()['status'],'failed')
        self.assertEqual(self.j.get(self.i)['phase'],'settled')
        self.i=replace(self.i,attempt_id='2')
        self.j.register(self.i,snapshot())
        self.cloud.ident=self.i;self.prep.ident=self.i;self.prep.fresh=True;self.platform.ident=self.i
        self.platform.fill_form=fill
        self.assertEqual(self.run_job()['status'],'reviewing')
        self.assertTrue(self.platform.replace_matching_draft)

    def test_verified_old_failure_is_read_back_before_replacing_its_draft(self):
        from dataclasses import replace
        self.closed=False
        self.platform.fail_click=True
        # An old unknown outcome must not authorize draft replacement.
        old=self.i
        self.assertEqual(self.run_job()['status'],'uncertain')
        self.cloud.resolved=True
        self.job.recover('token',self.i.user_id)
        self.assertEqual(self.j.get(old)['phase'],'settled')
        self.i=replace(self.i,attempt_id='2')
        self.j.register(self.i,snapshot())
        self.cloud.ident=self.i;self.prep.ident=self.i;self.prep.fresh=True;self.platform.ident=self.i
        self.platform.fail_click=False;self.closed=True
        self.assertEqual(self.run_job()['status'],'reviewing')
        self.assertTrue(self.platform.replace_matching_draft)
        self.assertTrue(self.j.get(old)['scene_resolution']['scene_resolved'])
