import unittest
from unittest.mock import Mock
from wt_media_agent.services.browser.http import ProfileHttpSession, ProfileHttpError, profile_proxy
from wt_media_agent.clients.baidu.publication import find_submission, BrowserReadRequired

class PublicationApiTests(unittest.TestCase):
 def test_proxy_is_explicit_and_socks_resolves_remotely(self):
  self.assertEqual(profile_proxy({'proxyType':'socks5','host':'127.0.0.1','port':1080,'proxyUserName':'a@b','proxyPassword':'p:x'}),'socks5h://a%40b:p%3Ax@127.0.0.1:1080')
  self.assertIsNone(profile_proxy({'proxyType':'noproxy'}))
  for c in [{},{'proxyType':'http','host':'','port':80},{'proxyType':'unsupported'}]:
   with self.assertRaises(ProfileHttpError):profile_proxy(c)
 def test_api_pages_find_exact_id_and_do_not_need_page_links(self):
  fetch=Mock(side_effect=[{'status':200,'json':{'errno':0,'data':{'list':[{'article_id':'1'}],'page':{'currentPage':1,'totalPage':2}}}},{'status':200,'json':{'errno':0,'data':{'list':[{'article_id':'2','url':'http://baijiahao.baidu.com/builder/preview/s?id=2'}],'page':{'currentPage':2,'totalPage':2}}}}])
  self.assertEqual(find_submission(fetch,'2',current=lambda:True)['article_id'],'2')
  self.assertEqual(fetch.call_count,2)
 def test_successful_empty_list_is_not_a_transport_error(self):
  fetch=Mock(return_value={'status':200,'json':{'errno':0,'data':{'list':[],'page':{'currentPage':1,'totalPage':0}}}})
  self.assertIsNone(find_submission(fetch,'2',current=lambda:True))
 def test_bad_page_is_not_counted_as_missing(self):
  fetch=Mock(return_value={'status':200,'json':{'errno':0,'data':{'list':[{'article_id':'1'}],'page':{'currentPage':2,'totalPage':2}}}})
  with self.assertRaises(ValueError):find_submission(fetch,'2',current=lambda:True)
 def test_authentication_error_requests_browser_fallback(self):
  with self.assertRaises(BrowserReadRequired):find_submission(lambda u:{'status':401,'json':{}},'2',current=lambda:True)

 def test_native_http_disables_environment_proxy_and_redirects(self):
  session=Mock();factory=Mock(return_value=session)
  response=session.get.return_value;response.status_code=200;response.content=b'{}';response.json.return_value={'errno':0}
  env={'proxyType':'http','host':'proxy.example','port':8080,'proxyUserName':'name','proxyPassword':'secret','browserFingerPrint':{'userAgent':'ProfileAgent'},'cookie':[{'name':'BDUSS','value':'private','domain':'.baidu.com'}]}
  client=ProfileHttpSession(env,session_factory=factory)
  client.get_json('https://baijiahao.baidu.com/pcui/article/lists?currentPage=1')
  factory.assert_called_once_with(trust_env=False)
  options=session.get.call_args.kwargs
  self.assertEqual(options['proxy'],'http://name:secret@proxy.example:8080')
  self.assertFalse(options['allow_redirects'])
  self.assertEqual(options['headers']['User-Agent'],'ProfileAgent')
  self.assertNotIn('Cookie',options['headers'])
  with self.assertRaises(ProfileHttpError):client.get_json('https://baijiahao.baidu.com.evil.test/pcui/article/lists')
 def test_missing_proxy_details_never_fall_back_to_direct(self):
  factory=Mock()
  with self.assertRaises(ProfileHttpError):ProfileHttpSession({'proxyType':'http','host':'','browserFingerPrint':{'userAgent':'Agent'}},session_factory=factory)
  factory.assert_not_called()
 def test_native_reader_matches_original_uid_and_requires_real_public_proof(self):
  from wt_media_agent.platforms.baidu.publication_api import BaiduPublicationApiReader
  from wt_media_agent.platforms.publish import PublicationReference
  reference=PublicationReference('6572476037','1882371730190912814',{'description':'title'})
  identity={'status':200,'json':{'data':{'user':{'user_id':'6572476037'}}}}
  row={'article_id':reference.submission_id,'url':'http://baijiahao.baidu.com/builder/preview/s?id='+reference.submission_id,'status':'review','is_published':0}
  listing={'status':200,'json':{'errno':0,'data':{'page':{'currentPage':1,'totalPage':1},'list':[row]}}}
  session=Mock();session.get_json.side_effect=[identity,listing]
  reader=BaiduPublicationApiReader({},session_factory=lambda env:session)
  result=reader.read(reference,current=lambda:True)
  self.assertEqual(result.conclusion,'reviewing');self.assertEqual(result.lookup_result,'found')
  self.assertTrue(result.target_url.startswith('https:'))
  session.close.assert_called_once()
  row['status']='publish';row['is_published']=1;session.get_json.side_effect=[identity,listing]
  with self.assertRaises(BrowserReadRequired) as error:reader.read(reference,current=lambda:True)
  self.assertEqual(error.exception.reason,'public_proof')
 def test_native_reader_foreign_uid_stops_before_querying_list(self):
  from wt_media_agent.platforms.baidu.publication_api import BaiduPublicationApiReader
  from wt_media_agent.platforms.publish import PublicationReference
  session=Mock();session.get_json.return_value={'status':200,'json':{'data':{'user':{'user_id':'foreign'}}}}
  result=BaiduPublicationApiReader({},session_factory=lambda env:session).read(PublicationReference('original','123',{}),current=lambda:True)
  self.assertEqual(result.lookup_result,'blocked');session.get_json.assert_called_once()
