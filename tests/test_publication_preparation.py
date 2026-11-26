from wt_media_agent.storage.database import Database
import tempfile
import unittest
from pathlib import Path
from wt_media_agent.storage.migration import apply_migrations
from wt_media_agent.storage.publication import PublicationJournal,ExecutionIdentity
from test_publication_execution import identity,snapshot

class PublicationPublicURLTests(unittest.TestCase):
 def test_public_baidu_work_query_survives_outbox(self):
  with tempfile.TemporaryDirectory() as tmp:
   db=Path(tmp)/'agent.db';apply_migrations(db);j=PublicationJournal(Database(db));i=identity();j.register(i,snapshot())
   packet=j.save_result(i,{'status':'reviewing','platform_work_id':'1834567890123456789','work_url':'https://baijiahao.baidu.com/s?id=1834567890123456789','evidence':{'receipt_id':'1834567890123456789'}},report_seq=1)
   self.assertEqual(j.pending(i.cloud_env,i.user_id)[0]['packet'],packet)
 def test_agent_cannot_report_a_public_success_or_control_state(self):
  with tempfile.TemporaryDirectory() as tmp:
   db=Path(tmp)/'agent.db';apply_migrations(db);j=PublicationJournal(Database(db));i=identity();j.register(i,snapshot())
   for status in ('published','cancelled','awaiting_human'):
    with self.assertRaises(ValueError):j.save_result(i,{'status':status,'evidence':{}},report_seq=1)
   self.assertEqual(j.pending(i.cloud_env,i.user_id),[])
 def test_private_grants_still_cannot_enter_outbox(self):
  with tempfile.TemporaryDirectory() as tmp:
   db=Path(tmp)/'agent.db';apply_migrations(db);j=PublicationJournal(Database(db));i=identity();j.register(i,snapshot())
   for url in ['https://baijiahao.baidu.com/s?id=1&token=x','https://store.test/v?signature=x','https://baijiahao.baidu.com/s?id=1&id=2','http://baijiahao.baidu.com/s?id=123']:
    with self.assertRaises(ValueError):j.save_result(i,{'status':'uncertain','work_url':url},report_seq=1)
   self.assertEqual(j.pending(i.cloud_env,i.user_id),[])

import copy
import concurrent.futures
from types import SimpleNamespace
from unittest.mock import patch
from wt_media_agent.storage.publication import JournalConflict
from wt_media_agent.storage.publication import PublicationClaimStore
from wt_media_agent.runner.publication_preparation import PublicationPreparation
from wt_media_agent.platforms.publish import PublicationInputError, PublishPayload, PublishControl
from wt_media_agent.platforms.baidu.publisher import BaiduPublisher

def cloud_claim(replayed=False, owner='3'):
 s=snapshot();content=dict(s['content'],cover_file_id='1',confirmed=True)
 frozen={k:v for k,v in s.items() if k not in ('platform','content')}
 frozen.update(profile_id='37',bit_profile_id='actual-bit-profile',bit_main_user_id='main-3',content=content)
 return {'task':{'id':'9','operator_id':owner,'platform':'baidu','current_attempt_id':'1','status':'running'},
         'attempt':{'id':'1','publication_id':'9','executor_user_id':owner,'status':'running','stage':'claimed','input_snapshot':frozen},
         'replayed':replayed,'files':{name:{'url':'https://storage.test/'+name,'sha256':s[name+'_sha256'],'size':s[name+'_size']} for name in ('video','cover','portrait_cover')}}

class FakeCloud:
 base_url='https://cloud.test'
 def __init__(self):self.value=cloud_claim();self.keys=[];self.reports=[];self.fail_claim=False;self.fail_report=False
 def claim_publication(self,token,key,*,capacity=None):
  self.keys.append(key)
  if self.fail_claim:raise ConnectionError('lost response')
  return copy.deepcopy(self.value)
 def report_publication(self,token,task,operation,packet):
  self.reports.append((task,operation,copy.deepcopy(packet)))
  if self.fail_report:raise ConnectionError('lost response')
  return {'stop_requested':False}
class FakeBit:
 def __init__(self):self.scans=0
 def scan_profiles(self):self.scans+=1;return SimpleNamespace(profiles=[SimpleNamespace(bit_profile_id='actual-bit-profile',main_user_id='main-3')])
 def open_profile_with_devtools(self, profile):return '127.0.0.1:9999'

class PublicationPreparationTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.db=Path(self.tmp.name)/'a.db';apply_migrations(self.db)
  self.j=PublicationJournal(Database(self.db));self.keys=PublicationClaimStore(Database(self.db));self.cloud=FakeCloud();self.bit=FakeBit();self.p=self.new_preparer()
 def new_preparer(self):return PublicationPreparation(self.cloud,self.bit,self.j,self.keys,Path(self.tmp.name)/'files',cloud_env='dev')
 def test_template_cover_audit_keeps_actual_frozen_file_and_confirmed_text(self):
  self.cloud.value['attempt']['input_snapshot']['content']['cover_render']={
   'template_id':'9007199254740993','published_revision':2,'title':'封面主标题',
   'background_output_id':'42','background_image_asset_id':'0','render_input_hash':'a'*64}
  value,i=self.p.claim('memory-token','3')
  saved=self.j.get(i)['input']
  self.assertEqual(saved['content'],snapshot()['content'])
  self.assertEqual(saved['cover_sha256'],value['attempt']['input_snapshot']['cover_sha256'])
  self.assertEqual(value['attempt']['input_snapshot']['content']['cover_render']['template_id'],'9007199254740993')
  self.assertEqual(self.bit.scans,0)
 def test_parallel_request_key_is_one_durable_identity(self):
  with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:values=list(pool.map(lambda _:PublicationClaimStore(Database(self.db)).pending('dev','3'),range(16)))
  self.assertEqual(len(set(values)),1)
  self.assertNotEqual(self.keys.pending('dev','4'),values[0])
  self.assertNotEqual(self.keys.pending('prod','3'),values[0])
 def test_lost_claim_reuses_original_key_and_replay_never_enters_browser(self):
  self.cloud.fail_claim=True
  with self.assertRaises(ConnectionError):self.p.claim('memory-token','3')
  self.cloud.fail_claim=False;self.cloud.value['replayed']=True
  with self.assertRaises(JournalConflict):self.new_preparer().claim('memory-token','3')
  self.assertEqual(self.cloud.keys[0],self.cloud.keys[1]);self.assertEqual(self.bit.scans,0)
  self.assertEqual(self.j.get(ExecutionIdentity('dev','3','9','1','actual-bit-profile'))['phase'],'claimed')
 def test_foreign_owner_and_malformed_claim_do_not_rotate_unknown_key(self):
  before=self.keys.pending('dev','3');self.cloud.value=cloud_claim(owner='4')
  with self.assertRaises(PublicationInputError):self.p.claim('memory-token','3')
  self.assertEqual(self.keys.pending('dev','3'),before);self.assertEqual(self.bit.scans,0)
 def test_restart_cannot_prepare_an_old_claim(self):
  value,i=self.p.claim('memory-token','3')
  with self.assertRaises(JournalConflict):self.new_preparer().prepare('memory-token',value,i)
  self.assertEqual(self.bit.scans,0)
 def test_wrong_digest_grant_is_rejected_before_browser_open(self):
  value,i=self.p.claim('memory-token','3');value['files']['video']['sha256']='c'*64
  with patch.object(self.bit, 'open_profile_with_devtools') as browser:
   with self.assertRaises(PublicationInputError):self.p.prepare('memory-token',value,i)
   browser.assert_not_called()
  self.assertEqual(self.cloud.reports,[])
 def test_lost_ready_response_remains_durable_and_never_reenters(self):
  value,i=self.p.claim('memory-token','3');self.cloud.fail_report=True
  with patch('wt_media_agent.runner.publication_preparation.download_verified'), patch.object(self.bit, 'open_profile_with_devtools', return_value='127.0.0.1:9999') as browser:
   prepared=self.p.prepare('memory-token',value,i)
   self.p.consume_prepared(prepared)
   publisher=BaiduPublisher(prepared.endpoint, identity_reader=lambda endpoint: {'login_status':'normal','platform_account_id':snapshot()['platform_uid']})
   payload=PublishPayload(prepared.video, prepared.cover, snapshot()['content'], snapshot()['platform_uid'])
   control=PublishControl(stopped=lambda:False, on_prepared=lambda:self.p.mark_ready('memory-token',i), before_submit=lambda:self.fail('cannot submit'))
   with self.assertRaises(ConnectionError):publisher.publish(payload,control)
   self.assertEqual(self.j.get(i)['phase'],'prepared')
   with self.assertRaises(JournalConflict):self.p.prepare('memory-token',value,i)
   self.assertEqual(browser.call_count,1)
  self.assertEqual(self.cloud.reports[0][2]['report_seq'],1)
 def test_preparing_report_before_download_does_not_block_the_same_fresh_preparation(self):
  value,i=self.p.claim('memory-token','3')
  self.assertEqual(self.j.reserve_report(i),1)
  with patch('wt_media_agent.runner.publication_preparation.download_verified'):
   prepared=self.p.prepare('memory-token',value,i)
  self.p.mark_ready('memory-token',i)
  self.assertEqual(self.j.get(i)['phase'],'prepared')
  self.assertEqual(self.cloud.reports[-1][2]['report_seq'],2)
  self.p.consume_prepared(prepared)
  with self.assertRaises(JournalConflict):self.p.consume_prepared(prepared)
 def test_wrong_live_uid_never_reports_ready(self):
  value,i=self.p.claim('memory-token','3')
  with patch('wt_media_agent.runner.publication_preparation.download_verified'):
   prepared=self.p.prepare('memory-token',value,i)
  self.p.consume_prepared(prepared)
  publisher=BaiduPublisher(prepared.endpoint, identity_reader=lambda endpoint: {'login_status':'normal','platform_account_id':'wrong'})
  payload=PublishPayload(prepared.video,prepared.cover,snapshot()['content'],snapshot()['platform_uid'])
  control=PublishControl(stopped=lambda:False,on_prepared=lambda:self.p.mark_ready('memory-token',i),before_submit=lambda:self.fail('cannot submit'))
  with self.assertRaises(PublicationInputError):publisher.publish(payload,control)
  self.assertEqual(self.cloud.reports,[])
 def test_session_loss_during_download_does_not_open_profile(self):
  from wt_media_agent.runner.publication_preparation import CloudExecutionStopped
  value,i=self.p.claim('memory-token','3');active=[True]
  def downloaded(*args,**kw):active[0]=False
  with patch('wt_media_agent.runner.publication_preparation.download_verified',side_effect=downloaded) as download, patch.object(self.bit,'open_profile_with_devtools') as browser:
   with self.assertRaises(CloudExecutionStopped):self.p.prepare('memory-token',value,i,current=lambda:active[0])
   self.assertEqual(download.call_count,1);browser.assert_not_called()
 def test_three_files_share_one_preparation_deadline(self):
  value,i=self.p.claim('memory-token','3')
  with patch('wt_media_agent.runner.publication_preparation.download_verified') as download:
   self.p.prepare('memory-token',value,i)
  self.assertEqual(download.call_count,3)
  self.assertEqual(len({call.kwargs['deadline'] for call in download.call_args_list}),1)
 def test_healthy_twenty_minute_resource_preparation_can_finish_before_browser_open(self):
  from wt_media_agent.services.files.verified_download import ResourceVerificationError
  value,i=self.p.claim('memory-token','3');clock=[0.0]
  def downloading(grant,target,*,current,deadline):
   clock[0]+=1200 if target.name=='video.mp4' else 1
   if not current() or clock[0]>=deadline:raise ResourceVerificationError('deadline reached')
  with patch('wt_media_agent.runner.publication_preparation.time.monotonic',side_effect=lambda:clock[0]), patch('wt_media_agent.runner.publication_preparation.download_verified',side_effect=downloading), patch.object(self.bit,'open_profile_with_devtools',return_value='127.0.0.1:9999') as browser:
   prepared=self.p.prepare('memory-token',value,i)
   self.assertEqual(prepared.endpoint,'127.0.0.1:9999')
   browser.assert_called_once()
