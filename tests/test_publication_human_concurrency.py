"""Multiple original human stacks backed by real SQLite; no platform acceptance."""
import tempfile
import unittest
from pathlib import Path
from threading import Event, Thread, get_ident
from types import SimpleNamespace
from unittest.mock import Mock
from wt_media_agent.storage.database import Database
from wt_media_agent.storage.migration import apply_migrations
from wt_media_agent.storage.publication import PublicationJournal, PublicationClaimStore, ExecutionIdentity, JournalConflict
from wt_media_agent.runner.publication_preparation import PublicationPreparation
from wt_media_agent.runner.publication_job import PublicationJob
from test_publication_execution import snapshot

class HumanConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(Path(self.tmp.name)/'a.db');apply_migrations(self.db);self.addCleanup(self.db.close)
        self.j=PublicationJournal(self.db)
        self.ids=[ExecutionIdentity('dev','3',str(n),str(n),f'profile-{n}') for n in (11,12)]
        for i in self.ids:self.j.register(i,snapshot())
        self.parked={i.task_id:Event() for i in self.ids};self.shown={i.task_id:Event() for i in self.ids}
        self.pulses={i.task_id:Event() for i in self.ids};self.release_report=Event();self.addCleanup(self.release_report.set)
        self.block_task=None;self.park_ack=True;self.resume_ack=True;self.lose_park=False
        self.cloud=Mock();self.cloud.report_publication.side_effect=self.report
        self.cloud.publication_human.side_effect=self.human
        self.cloud.original_publication_attempt.side_effect=ConnectionError('not needed')
        self.bit=SimpleNamespace(close_profile_verified=lambda *args:True)
        self.prep=PublicationPreparation(self.cloud,self.bit,self.j,PublicationClaimStore(self.db),self.tmp.name,cloud_env='dev')
        self.prep.prepare=lambda token,claim,i,**kw:SimpleNamespace(identity=i,video=Path('video'),cover=Path('cover'),portrait_cover=Path('portrait'),endpoint=i.task_id)
        self.prep.consume_prepared=lambda prepared:None
        self.threads=[];self.results={};self.shown_threads={};self.worker_threads={}
        outer=self
        class Publisher:
            def __init__(self,endpoint):self.task=endpoint
            def publish(self,payload,control,**kwargs):
                outer.worker_threads[self.task]=get_ident()
                control.on_prepared()
                def show():
                    outer.shown_threads[self.task]=get_ident();outer.shown[self.task].set()
                control.wait_for_human(show)
                control.before_submit()
                return {'platform_work_id':'1834567890123456789'}
        self.job=PublicationJob(self.prep,self.bit,lambda _:Publisher,heartbeat_interval=.02,capacity={'automatic_slots':5,'window_slots':10})
    def tearDown(self):
        self.release_report.set()
        for thread,stop in self.threads:stop.set();thread.join(3)
    def report(self,token,task,operation,packet):
        if operation=='complete':return {'attempt_id':packet['attempt_id'],'result_hash':packet['result_hash'],'task_status':packet['result']['status']}
        if packet.get('stage')=='awaiting_human':
            self.assertTrue(packet.get('scene_parked'))
            self.parked[task].set()
            if self.lose_park:raise TimeoutError('parking ack lost')
            return {'stop_requested':False,'allow_submit':False,'scene_parked':self.park_ack}
        if 'stage' not in packet:
            self.pulses[task].set()
            if task==self.block_task:self.release_report.wait(3)
        return {'stop_requested':False,'allow_submit':packet.get('stage')=='submitting'}
    def human(self,token,task,attempt,revision,*,resume,capacity=None):
        self.assertEqual(capacity,{'automatic_slots':5,'window_slots':10})
        return {'attempt_id':attempt,'stop_requested':False,'allow_submit':False,
                'task_status':'running' if resume else 'awaiting_human','scene_parked':False if resume and self.resume_ack else True}
    def start(self,i):
        stop=Event();source=snapshot();source.pop('platform');source['bit_profile_id']=i.bit_profile_id
        claim={'task':{'platform':'baidu'},'attempt':{'input_snapshot':source}}
        worker=Thread(target=lambda:self.results.update({i.task_id:self.job.run('token',claim,i,current=lambda:not stop.is_set())}),daemon=True)
        self.threads.append((worker,stop));worker.start();self.assertTrue(self.parked[i.task_id].wait(2))
        return worker
    def action(self,i,resume=True,token='token',attempt=None):
        for _ in range(100):
            try:return self.job.handle_human_action(token,i.user_id,i.task_id,attempt or i.attempt_id,'5',resume=resume)
            except JournalConflict:
                if token!='token' or attempt:return self.job.handle_human_action(token,i.user_id,i.task_id,attempt or i.attempt_id,'5',resume=resume)
                Event().wait(.01)
        self.fail('original parked scene not available')
    def test_two_scenes_show_and_resume_on_their_original_threads(self):
        threads=[self.start(i) for i in self.ids]
        for i in self.ids:self.action(i,resume=False);self.assertTrue(self.shown[i.task_id].wait(2))
        for i in self.ids:self.assertEqual(self.shown_threads[i.task_id],self.worker_threads[i.task_id])
        with self.assertRaises(JournalConflict):self.action(self.ids[0],token='old-token')
        with self.assertRaises(JournalConflict):self.action(self.ids[0],attempt='99')
        self.action(self.ids[0]);threads[0].join(2);self.assertFalse(threads[0].is_alive());self.assertTrue(threads[1].is_alive())
        self.action(self.ids[1]);threads[1].join(2);self.assertFalse(threads[1].is_alive())
        self.assertEqual([self.results[i.task_id]['status'] for i in self.ids],['reviewing','reviewing'])
    def test_missing_capacity_ack_does_not_wake_original_stack(self):
        thread=self.start(self.ids[0]);self.resume_ack=False
        with self.assertRaises(JournalConflict):
            self.job.handle_human_action('token','3','11','11','5',resume=True)
        self.assertTrue(thread.is_alive());self.assertEqual(self.j.get(self.ids[0])['phase'],'prepared')
        self.resume_ack=True;self.action(self.ids[0]);thread.join(2);self.assertFalse(thread.is_alive())
    def test_lost_parking_ack_never_authorizes_human_resume(self):
        self.lose_park=True
        thread=self.start(self.ids[0]);thread.join(2)
        self.assertFalse(thread.is_alive())
        with self.assertRaises(JournalConflict):self.job.handle_human_action('token','3','11','11','5',resume=True)
        self.assertNotEqual(self.results['11']['status'],'reviewing')
    def test_slow_report_of_one_attempt_does_not_block_other_heartbeat(self):
        self.block_task='11';self.start(self.ids[0]);self.assertTrue(self.pulses['11'].wait(2))
        self.start(self.ids[1]);self.assertTrue(self.pulses['12'].wait(1))
    def test_session_change_closes_all_original_waiting_stacks(self):
        threads=[self.start(i) for i in self.ids]
        for thread,stop in self.threads:stop.set()
        for thread in threads:thread.join(2);self.assertFalse(thread.is_alive())
        self.assertTrue(all(self.results[i.task_id]['evidence']['scene_closed'] for i in self.ids))
        self.assertFalse(self.job._human)
        self.cloud.publication_human.assert_not_called()
    def test_missing_legacy_park_flag_never_confirms_reliable_wait(self):
        original=self.report
        def missing_flag(token,task,operation,packet):
            result=original(token,task,operation,packet)
            if packet.get('scene_parked'):result.pop('scene_parked',None)
            return result
        self.cloud.report_publication.side_effect=missing_flag
        thread=self.start(self.ids[0]);thread.join(2);self.assertFalse(thread.is_alive())
        self.cloud.publication_human.assert_not_called()
        self.assertNotEqual(self.results['11']['status'],'reviewing')
