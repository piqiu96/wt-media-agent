import unittest
from unittest.mock import patch
from wt_media_agent.services.browser.account_cookie import parse_input, scoped, fingerprint, execute

class AccountCookieTest(unittest.TestCase):
    def test_scope_and_full_attributes(self):
        cookies=[{'name':'s','value':'old','domain':'.baidu.com','path':'/x','httpOnly':True,'secure':True,'sameSite':'Lax','expires':2000000000},{'name':'other','value':'v','domain':'.bilibili.com','path':'/'}]
        self.assertEqual(scoped(cookies,'baidu'),cookies[:1])
        self.assertEqual(parse_input('baidu', __import__('json').dumps(cookies[:1])), cookies[:1])
        with self.assertRaises(ValueError):parse_input('baidu', __import__('json').dumps(cookies))
        self.assertEqual(parse_input('baidu','BDUSS=abc==')[0]['value'],'abc==')
        self.assertEqual(parse_input('baidu','[{"name":"","value":"placeholder","domain":"baijiahao.baidu.com","path":"/","sourceScheme":"Secure","priority":"Medium"}]')[0]['sourceScheme'],'Secure')
        self.assertNotEqual(fingerprint(cookies[:1]),fingerprint([{**cookies[0],'path':'/'}]))
    def run_stage(self,stage,before,after=None,**extra):
        b=type('Browser',(),{'open_profile_with_devtools':lambda s,p:'127.0.0.1:1'})()
        body={'operation_id':'op','profile_id':'profile','platform':'baidu','input_cookie':'BDUSS=new','expected_platform_account_id':'1','fingerprint':fingerprint(before),**extra}
        with patch('wt_media_agent.services.browser.account_cookie.cdp.read_live_cookies',side_effect=[before,before,after if after is not None else before]),patch('wt_media_agent.services.browser.account_cookie.cdp.write_cookies') as write:
            r=execute(b,stage,body,lambda *args:{'login_status':'normal','platform_account_id':'1','name':'test'})
            return r,write.call_count
    def test_changed_snapshot_never_writes(self):
        r,n=self.run_stage('apply',[{'name':'BDUSS','value':'old','domain':'.baidu.com','path':'/'}],fingerprint='stale')
        self.assertEqual((r['status'],n),('failed',0))
    def test_inspect_never_writes(self):
        r,n=self.run_stage('inspect',[{'name':'BDUSS','value':'new','domain':'.baidu.com','path':'/'}])
        self.assertEqual((r['status'],n),('applied',0))
    def test_apply_readback_and_other_platform(self):
        other={'name':'s','value':'keep','domain':'.bilibili.com','path':'/'}
        old={'name':'BDUSS','value':'old','domain':'.baidu.com','path':'/'}
        new={**old,'value':'new'}
        r,n=self.run_stage('apply',[old,other],[new,other],fingerprint=fingerprint([old]))
        self.assertEqual((r['status'],n),('applied',1))
        r,n=self.run_stage('apply',[old,other],[new,{**other,'value':'changed'}],fingerprint=fingerprint([old]))
        self.assertEqual(r['status'],'uncertain')
    def test_identity_mismatch_refuses_write(self):
        before=[{'name':'BDUSS','value':'old','domain':'.baidu.com','path':'/'}]
        r,n=self.run_stage('apply',before,[{**before[0],'value':'new'}],expected_platform_account_id='2')
        self.assertEqual((r['status'],n),('failed',0))

class CDPFrameTests(unittest.TestCase):
    def test_fragmented_large_cookie_response_and_next_message(self):
        import json,struct
        from wt_media_agent.services.browser.cdp import _ws_recv
        content=json.dumps({'id':1,'result':{'cookies':[{'name':'s','value':'x'*70000}]}}).encode()
        def frame(data,opcode=1,final=True):
            first=(128 if final else 0)|opcode
            length=len(data)
            header=bytes([first,126])+struct.pack('>H',length) if length<65536 else bytes([first,127])+struct.pack('>Q',length)
            return header+data
        wire=bytearray(frame(content[:40000],final=False)+frame(content[40000:],opcode=0)+frame(b'{"id":2}'))
        class Socket:
            def settimeout(self,t):pass
            def recv(self,n):
                size=min(n,97);value=bytes(wire[:size]);del wire[:size];return value
        sock=Socket()
        self.assertEqual(_ws_recv(sock)['result']['cookies'][0]['value'],'x'*70000)
        self.assertEqual(_ws_recv(sock),{'id':2})
