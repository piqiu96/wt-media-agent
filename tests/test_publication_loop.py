from wt_media_agent.storage.database import Database
"""The polling loop is owner-routed; Cloud still decides all permissions."""
import base64,json,time,unittest
from types import SimpleNamespace
from wt_media_agent.runtime.agent_token import AgentToken
from wt_media_agent.clients.cloud.errors import AgentAuthError
from wt_media_agent.runner.publication_loop import PublicationLoop, routing_user_id


def token(user=3,exp=None,**extra):
    body=dict(sub=user,exp=int(time.time())+3600 if exp is None else exp,scope='agent',ver=1,sid='desktop-session',**extra)
    encoded=base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip('=')
    return f'v1.{encoded}.test-signature-not-real'


class InlineThread:
    def __init__(self, *, target, **kwargs): self.target = target
    def start(self): self.target()
    def join(self, timeout=None): pass


class PublicationLoopTests(unittest.TestCase):
    def setUp(self):
        self.holder=AgentToken();self.calls=[];self.failure=None;self.claim=None
        def claim(t,u):
            self.calls.append(('claim',t,u))
            if self.failure:raise self.failure
            value, self.claim = self.claim, None
            return value
        def recover(t,u):self.calls.append(('recover',t,u))
        def run(t,c,i,*,current):self.calls.append(('run',t,c,i,current))
        self.prep=SimpleNamespace(claim=claim)
        self.job=SimpleNamespace(recover=recover,run=run)
        self.check=SimpleNamespace(tick=lambda t,u,**kw:self.calls.append(("check",t,u)) if kw["current"]() else None)
        self.loop=PublicationLoop(self.holder,self.prep,self.job,self.check, window_count=lambda: 0, thread_factory=InlineThread)
    def test_absent_or_malformed_token_never_calls_cloud(self):
        self.loop.tick();self.assertEqual(self.calls,[])
        for value in ('opaque','v1.not-json.s',token(user=True),token(user=0),token(exp=1)):
            self.holder.set(value);self.loop.tick();self.assertEqual(self.calls,[])
    def test_only_current_owner_is_recovered_and_claimed(self):
        t=token();self.holder.set(t);self.loop.tick()
        self.assertEqual(self.calls,[('recover',t,'3'),('claim',t,'3'),('check',t,'3')])
        other=token(user=8);self.holder.set(other);self.calls.clear();self.loop.tick()
        self.assertEqual(self.calls,[('recover',other,'8'),('claim',other,'8'),('check',other,'8')])
    def test_refused_token_can_reconcile_but_cannot_claim_until_replaced(self):
        t=token();self.holder.set(t);self.failure=AgentAuthError('AUTH_DESKTOP_REPLACED','refused')
        self.loop.tick();self.failure=None;self.calls.clear();self.loop.tick()
        self.assertEqual(self.calls,[('recover',t,'3')])
        fresh=token(jti='new');self.holder.set(fresh);self.calls.clear();self.loop.tick()
        self.assertEqual(self.calls,[('recover',fresh,'3'),('claim',fresh,'3'),('check',fresh,'3')])
    def test_claim_runs_once_and_token_change_revokes_new_browser_actions(self):
        t=token();self.holder.set(t);self.claim=({'task':'fixture'},'identity');self.loop.tick()
        run=next(call for call in self.calls if call[0]=='run');self.assertEqual(run[:4],('run',t,{'task':'fixture'},'identity'));self.assertTrue(run[4]())
        self.holder.set(token(user=8));self.assertFalse(run[4]())
    def test_token_replaced_during_recovery_never_claims(self):
        t=token();self.holder.set(t)
        self.job.recover=lambda t,u:self.holder.set(token(user=8))
        self.loop.tick();self.assertEqual(self.calls,[])
    def test_stop_disables_current_execution_and_new_polls(self):
        self.holder.set(token());self.claim=({},'i');self.loop.tick();current=next(call for call in self.calls if call[0]=='run')[4]
        self.loop.stop();self.assertFalse(current());self.calls.clear();self.loop.tick();self.assertEqual(self.calls,[])
    def test_unverified_routing_hint_is_not_an_authentication_result(self):
        self.assertEqual(routing_user_id(token(user=123)),'123')
        for value in ('v2.e30.s','v1.'+'A'*20000+'.s',token(user='3')):
            self.assertIsNone(routing_user_id(value))
