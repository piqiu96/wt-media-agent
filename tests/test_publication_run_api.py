import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock
from wt_media_agent.local_api.server import LocalApiServer,make_handler
from wt_media_agent.local_api.state import LocalAgentState
from wt_media_agent.storage.publication import JournalConflict

class RunAPITests(unittest.TestCase):
 def setUp(self):
  self.job=Mock()
  self.job.handle_human_action.return_value={'attempt_id':'2','status':'awaiting_human'}
  self.api=LocalApiServer(LocalAgentState(),bitbrowser=object(),auth_token='runtime',publication_job=self.job,agent_token=SimpleNamespace(get=lambda:'cloud-token'))
  self.http=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.api))
  self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
 def tearDown(self):
  self.http.shutdown();self.thread.join();self.http.server_close()
 def request(self,operation='scene',body=None,user='9',token='runtime'):
  conn=http.client.HTTPConnection(*self.http.server_address)
  conn.request('POST','/api/v1/publication-run/2/'+operation,json.dumps(body if body is not None else {'publication_id':'1','expected_revision':'5'}),{'Authorization':'Bearer '+token,'X-Publication-User-ID':user})
  r=conn.getresponse();result=r.status,json.loads(r.read());conn.close();return result
 def test_exact_path_owner_and_runtime_token(self):
  self.assertEqual(self.request()[0],200)
  self.job.handle_human_action.assert_called_once_with('cloud-token','9','1','2','5',resume=False)
  self.assertEqual(self.request(token='wrong')[0],401)
  self.assertEqual(self.request(user='')[0],403)
 def test_invalid_or_lost_scene_never_opens_browser(self):
  self.assertEqual(self.request(body={'publication_id':1,'expected_revision':'5'})[0],400)
  self.assertEqual(self.request(body={'publication_id':'1','expected_revision':'5','token':'foreign'})[0],400)
  self.assertEqual(self.request(operation='retry')[0],404)
  self.assertEqual(self.job.handle_human_action.call_count,0)
  self.job.handle_human_action.side_effect=JournalConflict('lost scene')
  self.assertEqual(self.request(operation='resume')[0],409)
