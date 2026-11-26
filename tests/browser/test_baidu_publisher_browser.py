"""Real Chrome/Playwright controls against isolated HTML, never platform acceptance."""
import base64
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from wt_media_agent.platforms.baidu.publisher import BrowserPublisher, PlatformFormError
from wt_media_agent.platforms.publish import PublishPayload
from wt_media_agent.services.browser.session import BrowserSession

CHROME = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
FIXTURE = Path(__file__).with_name('baidu-editor.html').read_text()


class BrowserPublisherBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not CHROME.is_file():
            raise RuntimeError('Google Chrome is required for the isolated browser checks')
        cls.profile = tempfile.TemporaryDirectory(prefix='wt-publisher-browser-')
        cls.browser = subprocess.Popen([str(CHROME), '--headless=new', '--no-first-run',
            '--no-default-browser-check', '--remote-debugging-port=0',
            '--user-data-dir='+cls.profile.name, 'about:blank'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = Path(cls.profile.name)/'DevToolsActivePort'
        deadline = time.monotonic()+15
        while not port_file.exists():
            if cls.browser.poll() is not None or time.monotonic()>deadline:
                cls.tearDownClass()
                raise RuntimeError('isolated Chrome did not start')
            time.sleep(.05)
        cls.endpoint='127.0.0.1:'+port_file.read_text().splitlines()[0]

    @classmethod
    def tearDownClass(cls):
        cls.browser.terminate()
        cls.browser.wait(timeout=10)
        cls.profile.cleanup()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)
        self.cover=self.folder/'cover'
        self.cover.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aoykAAAAASUVORK5CYII='))
        self.cover_response=self.cover.read_bytes()
        self.portrait_cover=self.folder/'portrait-cover'
        self.portrait_cover.write_bytes(self.cover_response)
        self.video=self.folder/'video.mp4'
        self.video.write_bytes(b'fixture video; not a real platform upload')
        self.payload=PublishPayload(self.video,self.cover,{'description':'确认描述','topics':'#测试话题','declaration':'无需声明'}, 'fixture-uid',portrait_cover=self.portrait_cover)
        self.session=BrowserSession(self.endpoint).__enter__()
        self.addCleanup(self.session.close)
        self.page=self.session.page
        self.external=[]
        self.home='<meta charset="utf-8"><a href="/builder/rc/edit">发布视频</a>'
        def isolated(route):
            request=route.request
            if request.url=='https://baijiahao.baidu.com/builder/rc/home':
                route.fulfill(content_type='text/html',body=self.home)
            elif request.url=='https://baijiahao.baidu.com/builder/rc/edit':
                route.fulfill(content_type='text/html',body=FIXTURE)
            elif request.url == 'https://baijiahao.baidu.com/materialui/video/compuploadvideo':
                route.fulfill(content_type='application/json',body='{"error_code":0,"mediaId":"fixture-new-video"}')
            elif request.url == 'https://baijiahao.baidu.com/pcui/video/process':
                route.fulfill(content_type='application/json',body='{"errno":0,"data":{"status":"PUBLISHED","img_frame":["fixture-frame"]}}')
            elif request.url == 'https://baijiahao.baidu.com/pcui/picture/processproxy':
                index = parse_qs(request.post_data)['index'][0]
                route.fulfill(content_type='application/json', body=json.dumps({
                    'errno': 0, 'ret': {'url': 'https://baijiahao.baidu.com/fixture-cover/' + index}}))
            elif request.url == 'https://baijiahao.baidu.com/fixture-recommendation':
                route.fulfill(content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60"><rect width="80" height="60" fill="gray"/></svg>')
            elif request.url.startswith('https://baijiahao.baidu.com/fixture-cover/'):
                route.fulfill(content_type='image/png',body=self.cover_response)
            elif request.url=='https://baijiahao.baidu.com/pcui/article/publish?type=video' and request.method=='POST':
                route.fulfill(content_type='application/json',body='{"errno":0,"ret":{"article_id":1862233438702758904}}')
            else:
                self.external.append(request.url)
                route.abort()
        self.page.route('**/*',isolated)
        self.page.goto('https://baijiahao.baidu.com/builder/rc/edit')
        self.page.evaluate('window.fixtureEditor()')
        self.publisher=BrowserPublisher(timeout=.5)

    def fill(self):
        self.publisher.fill_fields(self.page,self.payload,stopped=lambda:False)

    def check_public_reference(self, *, anonymous_public, link_scheme="https", decoy_links=(), list_pages=None, page_number_override=None, nonpreview_ids=()):
        from types import SimpleNamespace
        from unittest.mock import patch
        from wt_media_agent.platforms.baidu.publication_check import BaiduPublicationChecker
        from wt_media_agent.platforms.publication_check import PublicationCheckControl
        from wt_media_agent.services.browser.http import ProfileHttpError
        from contextlib import nullcontext
        from wt_media_agent.platforms.publication_check import PublicationReference
        import shutil
        encoder=shutil.which('ffmpeg')
        self.assertIsNotNone(encoder,'ffmpeg is required for real isolated media checks')
        video=self.folder/'public-proof.mp4'
        subprocess.run([encoder,'-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=black:s=32x32:d=1',
                        '-c:v','libx264','-pix_fmt','yuv420p','-movflags','faststart',str(video)],check=True)
        media=video.read_bytes()
        target='https://baijiahao.baidu.com/builder/preview/s?id=1882371730190912814'
        public='https://mbd.baidu.com/newspage/data/videolanding?nid=sv_7000395122085014122'
        media_url='https://mbd.baidu.com/fixture/public-proof.mp4'
        visits=[]
        list_pages=list_pages or [[target.replace('https:',link_scheme+':',1)]]
        list_visits=[]
        self.public_check_list_visits=list_visits
        def fixture(route):
            request=route.request
            if request.url=='https://baijiahao.baidu.com/builder/rc/content':
                body='<meta charset="utf-8"><div id="list"></div><ul><li class="cheetah-pagination-item-active" title="1"></li><li class="cheetah-pagination-next" title="下一页" aria-disabled="false"><button onclick="load(current+1)">下一页</button></li></ul><script>const pages='+json.dumps(list_pages)+';const decoys='+json.dumps(list(decoy_links))+';let current=1;async function load(n){await fetch("/pcui/article/lists?currentPage="+n).then(r=>r.json());await new Promise(resolve=>setTimeout(resolve,30));current=n;document.querySelector("#list").innerHTML=[...pages[n-1],...decoys].map(link=>`<a href="${link}">指定稿件</a>`).join("");document.querySelector(".cheetah-pagination-next").setAttribute("aria-disabled",String(n===pages.length));document.querySelector(".cheetah-pagination-item-active").setAttribute("title",String(n));}load(1);</script>'
                route.fulfill(content_type='text/html',body=body)
            elif urlsplit(request.url).hostname=='baijiahao.baidu.com' and urlsplit(request.url).path=='/pcui/article/lists':
                number=int(parse_qs(urlsplit(request.url).query)['currentPage'][0]);list_visits.append(number)
                records=[{'article_id':parse_qs(urlsplit(link).query)['id'][0],'url':link} for link in list_pages[number-1]]+[{'article_id':value} for value in nonpreview_ids]
                route.fulfill(content_type='application/json',body=json.dumps({'errno':0,'data':{'list':records,'page':{'currentPage':(page_number_override or {}).get(number,number),'pageSize':10,'totalPage':len(list_pages),'totalCount':sum(map(len,list_pages))}}}))
            elif request.url==target:
                self.external.append(request.url);route.abort()
            elif request.url==public:
                authenticated='fixture_session=private-test' in request.all_headers().get('cookie','')
                visits.append(authenticated)
                if not anonymous_public and not authenticated:
                    route.fulfill(status=403,body='Login required')
                else:
                    metadata={'id':'7000395122085014122','title':'确认描述 #测试话题','publish_time':1795169573,'duration':1,'playurl':media_url}
                    route.fulfill(content_type='text/html',body='<meta charset="utf-8"><script>window.fixturePublic={curVideoMeta:'+json.dumps(metadata,ensure_ascii=False)+'};</script><video preload="metadata" src="'+media_url+'"></video>')
            elif request.url==media_url:
                route.fulfill(content_type='video/mp4',body=media)
            else:
                self.external.append(request.url);route.abort()
        self.page.unroute('**/*')
        self.page.context.route('**/*',fixture)
        self.page.context.add_cookies([{'name':'fixture_session','value':'private-test','domain':'.baidu.com','path':'/','secure':True}])
        browser=self.page.context.browser
        original=browser.new_context
        contexts=[]
        def anonymous_context(**kwargs):
            self.assertEqual(kwargs['storage_state'],{'cookies':[],'origins':[]})
            context=original(**kwargs)
            context.route('**/*',fixture)
            contexts.append(context)
            return context
        page=self.page
        class OwnedSession:
            def __enter__(self):return SimpleNamespace(page=page)
            def __exit__(self,*_):pass
        reader=BaiduPublicationChecker(browser_session_factory=lambda _:OwnedSession(),
            identity_reader=lambda _:{'login_status':'normal','platform_account_id':'fixture-uid'})
        original_goto=page.goto
        navigations=[]
        self.public_check_navigations=navigations
        def navigate(url, **kwargs):
            navigations.append(url)
            # Simulate the HTTP redirect boundary; Playwright route handlers do
            # not intercept redirected requests. All browser traffic stays isolated.
            return original_goto(public if url==target else url, **kwargs)
        with patch.object(browser,'new_context',side_effect=anonymous_context), patch.object(page,'goto',side_effect=navigate):
            result=reader.check(PublicationReference('fixture-uid','1882371730190912814',dict(self.payload.content)),PublicationCheckControl(
                current=lambda:True, browser_fallback_allowed=True,
                read_environment=lambda:(_ for _ in ()).throw(ProfileHttpError('fixture browser required')),
                browser=lambda:nullcontext(self.endpoint)))
        if result.conclusion=='uncertain':
            self.assertEqual(navigations,['https://baijiahao.baidu.com/pcui/article/lists?currentPage=1&pageSize=10'])
            self.assertEqual(visits,[])
            self.assertEqual(len(contexts),0)
            self.assertEqual(result.work_id,'')
            self.assertEqual(result.work_url,'')
            self.assertIsNone(result.published_at)
        else:
            self.assertEqual(navigations,['https://baijiahao.baidu.com/pcui/article/lists?currentPage=1&pageSize=10',target])
            self.assertEqual(visits,[True,False])
            self.assertEqual(result.target_url,target)
        self.assertEqual(len(browser.contexts),1)
        self.assertEqual(self.external,[])
        self.assertEqual(list_visits,list(range(1,len(list_pages)+1)))
        return result

    def test_exact_original_link_and_real_anonymous_media_prove_public_work(self):
        result=self.check_public_reference(anonymous_public=True)
        self.assertEqual(result.conclusion,'published')
        self.assertEqual(result.work_id,'7000395122085014122')
        self.assertEqual(result.published_at.isoformat(),'2026-11-20T10:12:53+00:00')

    def test_observed_http_receipt_link_is_upgraded_before_public_verification(self):
        target='http://baijiahao.baidu.com/builder/preview/s?id=1882371730190912814'
        result=self.check_public_reference(anonymous_public=True, link_scheme='http', decoy_links=(
            target.replace('baijiahao.baidu.com','baijiahao.baidu.com.evil.test'),
            target.replace('baijiahao.baidu.com','fixture-user:fixture-password@baijiahao.baidu.com'),
            target+'&token=fixture-private', target+'#changed',
            target.replace('1882371730190912814','1882359387756944157')))
        self.assertEqual(result.conclusion,'published')
        self.assertTrue(result.target_url.startswith('https://'))

    def test_original_submission_on_second_page_is_verified_without_reposting(self):
        target='http://baijiahao.baidu.com/builder/preview/s?id=1882371730190912814'
        result=self.check_public_reference(anonymous_public=True,list_pages=[[
            target.replace('1882371730190912814','1882359387756944157')],[target]])
        self.assertEqual(result.conclusion,'published')
        self.assertEqual(result.work_id,'7000395122085014122')

    def test_unrelated_nonpreviewable_record_does_not_block_pagination(self):
        target='http://baijiahao.baidu.com/builder/preview/s?id=1882371730190912814'
        result=self.check_public_reference(anonymous_public=True,list_pages=[[
            target.replace('1882371730190912814','1882359387756944157')],[target]],
            nonpreview_ids=('1882000000000000001',))
        self.assertEqual(result.conclusion,'published')
        self.assertEqual(result.work_id,'7000395122085014122')

    def test_missing_original_does_not_use_another_work_after_last_page(self):
        other='http://baijiahao.baidu.com/builder/preview/s?id=1882359387756944157'
        result=self.check_public_reference(anonymous_public=True,list_pages=[[other],[other.replace('4157','4158')]])
        self.assertEqual(result.conclusion,'uncertain')
        self.assertEqual(result.reason,'原账号内容列表未找到指定投稿')
        self.assertIn('不能据此认定投稿失败',result.basis)
        self.assertEqual(self.public_check_list_visits,[1,2])
        self.assertEqual(self.public_check_navigations,['https://baijiahao.baidu.com/pcui/article/lists?currentPage=1&pageSize=10'])
        self.assertEqual(self.external,[])

    def test_repeated_page_number_does_not_loop_or_register_another_work(self):
        from wt_media_agent.platforms.publication_check import PublicationCheckError
        other='http://baijiahao.baidu.com/builder/preview/s?id=1882359387756944157'
        target=other.replace('1882359387756944157','1882371730190912814')
        with self.assertRaises(PublicationCheckError):
            self.check_public_reference(anonymous_public=True,list_pages=[[other],[target]],page_number_override={2:1})
        self.assertEqual(self.public_check_list_visits,[1,2])
        self.assertEqual(self.public_check_navigations,['https://baijiahao.baidu.com/pcui/article/lists?currentPage=1&pageSize=10'])
        self.assertEqual(self.external,[])

    def test_authenticated_preview_with_anonymous_login_refusal_stays_reviewing(self):
        result=self.check_public_reference(anonymous_public=False)
        self.assertEqual(result.conclusion,'reviewing')
        self.assertEqual(result.work_id,'')
        self.assertIsNone(result.published_at)

    def test_metadata_without_decoded_video_frame_cannot_prove_public_work(self):
        from wt_media_agent.platforms.baidu.publication_check import PUBLIC_METADATA
        self.page.set_content('<video></video>')
        self.page.evaluate("""() => {
          const url='https://mbd.baidu.com/fixture/metadata-only.mp4';
          window.fixturePublic={curVideoMeta:{id:'7000395122085014122',title:'title',
            publish_time:1795169573,duration:30,playurl:url}};
          const video=document.querySelector('video');
          // Deterministically model HAVE_METADATA with no decoded current frame.
          for(const [key,value] of Object.entries({currentSrc:url,readyState:1,
              duration:30,videoWidth:0,videoHeight:0}))
            Object.defineProperty(video,key,{value});
        }""")
        self.assertFalse(self.page.evaluate(PUBLIC_METADATA,'7000395122085014122'))

    def test_fields_cover_dialogs_and_one_matching_receipt(self):
        self.fill()
        self.assertEqual(self.page.locator('[contenteditable]').inner_text(),'确认描述 #测试话题')
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)
        self.publisher.submit()
        self.assertEqual(self.publisher.read_receipt(),{'platform_work_id':'1862233438702758904'})
        with self.assertRaises(PlatformFormError):self.publisher.submit()
        self.assertEqual(self.page.evaluate('window.finalClicks'),1)
        self.assertEqual(self.external,[])

    def test_stop_and_changed_field_never_click_final(self):
        self.fill()
        self.publisher._stopped=lambda:True
        with self.assertRaises(PlatformFormError):self.publisher.submit()
        self.publisher._stopped=lambda:False
        self.page.locator('[contenteditable]').fill('unexpected edit')
        with self.assertRaises(PlatformFormError):self.publisher.submit()
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)

    def test_form_mismatch_diagnostic_identifies_only_the_changed_invariant(self):
        self.fill()
        self.page.locator('[contenteditable]').fill('private unexpected content')
        with self.assertLogs('wt_media_agent.platforms.baidu.publisher',level='INFO') as captured:
            with self.assertRaises(PlatformFormError):self.publisher.submit()
        self.assertIn('text_changed=True declaration_changed=False covers_changed=False dialog_changed=False', str(captured.output))
        self.assertNotIn('private unexpected content',str(captured.output))
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)

    def test_cover_configuration_waits_for_current_uploaded_video_processing(self):
        self.page.context.add_init_script('window.fixtureVideoProcessingDelayed=true')
        entered=[]
        self.publisher.timeout=3
        self.publisher.fill_fields=lambda *a,**kw: entered.append(self.page.evaluate('window.fixtureVideoProcessed===true'))
        self.publisher.fill_form(self.page,self.payload,stopped=lambda:False)
        self.assertEqual(entered,[True])

    def test_public_result_owner_recheck_uses_current_profile_context(self):
        from wt_media_agent.platforms.baidu.publication_check import BaiduPublicationChecker
        from wt_media_agent.platforms.publication_check import PublicationReference
        context = self.page.context
        context.route('https://image.baidu.com/user/logininfo?**', lambda route: route.fulfill(
            content_type='application/json', body='{"data":{"user":{"user_id":6572476037}}}'))
        before = len(context.pages)
        reader = BaiduPublicationChecker()
        reader._verify_owner(PublicationReference('6572476037','1882371730190912814',{}),
                             lambda: True, page=self.page)
        self.assertEqual(len(context.pages), before)
        self.assertFalse(self.page.is_closed())

    def test_identity_recheck_reuses_active_browser_context_and_closes_own_tab(self):
        from wt_media_agent.platforms.baidu.identity import read_baidu_identity
        context = self.page.context
        context.route('https://image.baidu.com/user/logininfo?**', lambda route: route.fulfill(
            content_type='application/json', body='{"data":{"user":{"user_id":6572476037}}}'))
        before = len(context.pages)
        self.assertEqual(read_baidu_identity(self.endpoint, browser_context=context),
                         {'login_status':'normal', 'platform_account_id':'6572476037'})
        self.assertEqual(len(context.pages), before)
        self.assertFalse(self.page.is_closed())

    def test_recommended_image_is_not_frozen_before_selected_cover_arrives(self):
        self.page.evaluate('window.fixtureIntermediateRecommendation=true')
        self.fill()
        self.assertTrue(all('/fixture-cover/' in source for source in self.publisher._covers))
        self.publisher.submit()
        self.assertEqual(self.page.evaluate('window.finalClicks'), 1)

    def test_temporary_cover_preview_is_not_frozen_before_platform_upload_finishes(self):
        self.fill()
        self.assertTrue(all(value.startswith('https://') for value in self.publisher._covers))
        self.page.wait_for_timeout(250)
        self.publisher.submit()
        self.assertEqual(self.publisher.read_receipt(),{'platform_work_id':'1862233438702758904'})
        self.assertEqual(self.page.evaluate('window.finalClicks'),1)

    def test_cover_without_platform_upload_completion_cannot_submit(self):
        self.page.evaluate('window.fixtureTemporaryCoversOnly=true')
        with self.assertRaises(PlatformFormError):self.fill()
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)

    def test_uploaded_cover_url_rotation_preserves_confirmed_image(self):
        self.fill()
        self.page.locator('[data-testid=cover-preview] img').first.evaluate("e=>e.src='https://baijiahao.baidu.com/fixture-cover/0?renewed=1#editor-preview'")
        self.page.wait_for_function("document.querySelector('[data-testid=cover-preview] img').complete")
        self.publisher.submit()
        self.assertEqual(self.publisher.read_receipt(),{'platform_work_id':'1862233438702758904'})
        self.assertEqual(self.page.evaluate('window.finalClicks'),1)

    def test_observed_http_cover_preview_preserves_confirmed_image(self):
        self.publisher.timeout = 2
        self.fill()
        self.page.route('http://baijiahao.baidu.com/fixture-cover/*',
                        lambda route: route.fulfill(content_type='image/png', body=self.cover_response))
        self.page.locator('[data-testid=cover-preview] img').first.evaluate(
            "e=>e.src='http://baijiahao.baidu.com/fixture-cover/0?http-preview=1'")
        self.page.wait_for_function("document.querySelector('[data-testid=cover-preview] img').complete")
        self.publisher.submit()
        self.assertEqual(self.publisher.read_receipt(), {'platform_work_id': '1862233438702758904'})
        self.assertEqual(self.page.evaluate('window.finalClicks'), 1)

    def test_http_dom_cover_matches_exact_browser_upgraded_response(self):
        self.publisher.timeout = 2
        self.fill()
        self.page.route('http://baijiahao.baidu.com/fixture-cover/*',
                        lambda route: route.fulfill(status=302, headers={
                            'location': route.request.url.replace('http:', 'https:', 1)}))
        self.page.locator('[data-testid=cover-preview] img').first.evaluate(
            "e=>e.src='http://baijiahao.baidu.com/fixture-cover/0?upgraded=1'")
        self.page.wait_for_function("document.querySelector('[data-testid=cover-preview] img').complete")
        self.publisher._image_responses = {key: value for key, value in
            self.publisher._image_responses.items() if not key.startswith('http:')}
        self.publisher.submit()
        self.assertEqual(self.page.evaluate('window.finalClicks'), 1)

    def test_changed_cover_bytes_never_submit_even_when_the_url_is_unchanged(self):
        self.fill()
        self.cover_response=b'changed upstream image bytes; same URL'
        self.page.evaluate("async()=>{const source=document.querySelector('[data-testid=cover-preview] img').src;await (await fetch(source,{cache:'reload'})).arrayBuffer()}")
        with self.assertRaises(PlatformFormError):self.publisher.submit()
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)

    def test_same_uploaded_cover_can_be_rendered_from_local_data_or_blob(self):
        self.fill()
        raw=base64.b64encode(self.cover_response).decode()
        self.page.locator('[data-testid=cover-preview] img').first.evaluate("(e, raw)=>e.src='data:image/png;base64,'+raw",raw)
        self.page.locator('[data-testid=cover-preview] img').nth(1).evaluate("async(e,raw)=>e.src=URL.createObjectURL(await (await fetch('data:image/png;base64,'+raw)).blob())",raw)
        self.publisher.submit()
        self.assertEqual(self.publisher.read_receipt(),{'platform_work_id':'1862233438702758904'})
        self.assertEqual(self.page.evaluate('window.finalClicks'),1)

    def test_submit_waits_for_the_current_cover_response_to_arrive(self):
        self.fill()
        responses=dict(self.publisher._image_responses)
        self.publisher._image_responses.clear()
        self.page.once('console',lambda _:self.publisher._image_responses.update(responses))
        self.page.evaluate("setTimeout(()=>console.log('fixture image response ready'),150)")
        self.publisher.submit()
        self.assertEqual(self.publisher.read_receipt(),{'platform_work_id':'1862233438702758904'})
        self.assertEqual(self.page.evaluate('window.finalClicks'),1)

    def test_local_path_over_50_mib_uses_actual_browser_file_input(self):
        with self.video.open('wb') as file:
            file.truncate(64*1024*1024)
        self.page.locator('input[type=file][accept=".mp4"]').set_input_files(str(self.video))
        self.assertEqual(self.page.evaluate('window.selectedVideoSize'),64*1024*1024)
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)
        self.assertEqual(self.external,[])

    def test_human_pause_keeps_same_editor_and_blocks_manual_publish(self):
        self.home+='<div class="passMod_dialog-wrapper">security verification</div>'
        waits=[]
        def human(show):
            waits.append(self.page.url)
            show()
            blocked=self.page.evaluate("fetch('/pcui/article/publish?type=video',{method:'POST'}).then(()=>false).catch(()=>true)")
            self.assertTrue(blocked)
            self.page.locator('.passMod_dialog-wrapper').evaluate('(node)=>node.remove()')
        self.publisher.fill_form(self.page,self.payload,stopped=lambda:False,wait_for_human=human)
        self.assertEqual(waits,['https://baijiahao.baidu.com/builder/rc/home'])
        self.assertEqual(self.page.evaluate('window.selectedVideoSize'),self.video.stat().st_size)
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)
        self.publisher.submit()
        self.assertEqual(self.publisher.read_receipt()['platform_work_id'],'1862233438702758904')
        self.assertEqual(self.page.evaluate('window.finalClicks'),1)
        self.assertEqual(self.external,[])

    def test_full_navigation_upload_and_fill_do_not_submit(self):
        self.publisher.fill_form(self.page,self.payload,stopped=lambda:False)
        self.assertEqual(self.page.evaluate('window.selectedVideoSize'),self.video.stat().st_size)
        self.assertEqual(self.page.locator('[contenteditable]').inner_text(),'确认描述 #测试话题')
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)

    def collapsed_home(self):
        self.home='''<meta charset="utf-8"><div id="home-publish-btn" onmouseenter="window.menuShown=true;document.querySelector('a').style.opacity=1;document.querySelector('a').style.pointerEvents='auto'">＋</div><a style="opacity:0;pointer-events:none" href="/builder/rc/edit">发布视频</a>'''

    def test_delayed_publish_menu_does_not_capture_home_card_locator(self):
        self.publisher.timeout = 2
        self.home='''<meta charset="utf-8"><div id="home-statistic-card"><span>发布视频</span></div>
        <div id="home-publish-btn" onmouseenter="setTimeout(()=>document.body.insertAdjacentHTML('beforeend', '<div class=&quot;cheetah-popover-inner&quot;><a href=&quot;/builder/rc/edit&quot;>发布视频</a></div>'),200)">＋</div>'''
        self.publisher.fill_form(self.page,self.payload,stopped=lambda:False)
        self.assertEqual(self.page.locator('[contenteditable]').inner_text(), '确认描述 #测试话题')
        self.assertEqual(self.page.evaluate('window.finalClicks'), 0)
        self.assertEqual(self.external, [])

    def test_duplicate_home_card_and_menu_labels_select_open_menu(self):
        self.publisher.timeout = 2
        self.home='''<meta charset="utf-8"><div id="home-statistic-card"><span onclick="window.wrongEntry=true">发布视频</span></div>
        <div id="home-publish-btn" onmouseenter="document.querySelector('.cheetah-popover-inner').hidden=false">＋</div>
        <div class="cheetah-popover-inner" hidden><a href="/builder/rc/edit">发布视频</a></div>'''
        self.publisher.fill_form(self.page,self.payload,stopped=lambda:False)
        self.assertEqual(self.page.locator('[contenteditable]').inner_text(),'确认描述 #测试话题')
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)
        self.assertEqual(self.external,[])

    def test_collapsed_home_menu_is_opened_before_upload(self):
        self.collapsed_home()
        self.publisher.fill_form(self.page,self.payload,stopped=lambda:False)
        self.assertEqual(self.page.evaluate('window.selectedVideoSize'),self.video.stat().st_size)
        self.assertEqual(self.page.locator('[contenteditable]').inner_text(),'确认描述 #测试话题')
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)
        self.assertEqual(self.external,[])

    def test_portal_placement_is_ready_before_click_without_diagnostic_delay(self):
        self.home='''<meta charset="utf-8"><div id="home-publish-btn" style="width:40px;height:40px"
        onmouseleave="if(!window.placed){const a=document.querySelector('a');a.style.opacity=0;a.style.pointerEvents='none'}"
        onmouseenter="const a=document.querySelector('a');a.style.opacity=1;a.style.pointerEvents='auto';
        setTimeout(()=>{window.placed=true;a.style.top='100px'},1200)">＋</div>
        <a href="/builder/rc/edit" style="position:absolute;top:1400px;opacity:0;pointer-events:none">发布视频</a>
        <script>window.addEventListener('scroll',()=>{if(!window.placed){
        const a=document.querySelector('a');a.style.opacity=0;a.style.pointerEvents='none'}})</script>'''
        self.publisher._record_failure=lambda *args,**kwargs:None
        self.publisher.timeout=2
        self.publisher.fill_form(self.page,self.payload,stopped=lambda:False)
        self.assertEqual(self.page.evaluate('window.selectedVideoSize'),self.video.stat().st_size)
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)
        self.assertEqual(self.external,[])

    def test_stop_after_opening_home_menu_never_enters_editor(self):
        self.collapsed_home()
        with self.assertRaises(PlatformFormError):
            self.publisher.fill_form(self.page,self.payload,stopped=lambda:self.page.evaluate('Boolean(window.menuShown)'))
        self.assertTrue(self.page.evaluate('window.menuShown'))
        self.assertTrue(self.page.url.endswith('/builder/rc/home'))
        self.assertEqual(self.external,[])

    def test_delayed_portrait_preview_uses_poll_budget_instead_of_action_timeout(self):
        self.page.evaluate('window.fixtureDelayedPortrait=true')
        self.page.set_default_timeout(200)
        self.publisher.timeout=2
        self.fill()
        self.assertEqual(self.page.locator('[data-testid=cover-preview] img').count(),2)
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)

    def test_stop_while_portrait_preview_is_missing_remains_responsive(self):
        self.page.evaluate('window.fixtureDelayedPortrait=true')
        self.page.set_default_timeout(200)
        self.publisher.timeout=2
        checks=0
        def stopped():
            nonlocal checks
            if self.page.evaluate('Boolean(window.fixturePreviewWaiting)'):
                checks+=1
            return checks>=3
        with self.assertRaises(PlatformFormError):
            self.publisher.fill_fields(self.page,self.payload,stopped=stopped)
        self.assertEqual(checks,3)
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)

    def test_existing_preview_waits_for_the_selected_cover_replacement(self):
        self.page.evaluate('window.fixtureStalePreview=true')
        previous=self.page.locator('[data-testid=cover-preview] img').first.get_attribute('src')
        self.publisher.timeout=3
        self.fill()
        self.page.wait_for_function('old=>document.querySelector(\'[data-testid=cover-preview] img\').src!==old',arg=previous)
        actual=self.page.locator('[data-testid=cover-preview] img').evaluate_all('(es)=>es.map(e=>e.src)')
        self.assertEqual(self.publisher._covers,actual)
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)

    def test_unchanged_existing_preview_cannot_confirm_a_new_cover(self):
        self.page.evaluate('window.fixtureNeverPreview=true')
        self.publisher.timeout=.3
        with self.assertRaises(PlatformFormError):self.fill()
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)

    def test_cover_failure_or_security_check_stops_without_submission(self):
        self.page.evaluate("document.querySelector('#dialogs').addEventListener('change',e=>e.stopPropagation(),true)")
        # An invalid image never decodes, so form completion must time out.
        self.cover.write_bytes(b'\xff\xd8\xffinvalid fixture image')
        with self.assertRaises(PlatformFormError):self.fill()
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)
        self.page.evaluate("document.body.insertAdjacentHTML('beforeend','<div class=passMod_dialog-wrapper>security verification</div>')")
        with self.assertRaisesRegex(PlatformFormError,'human security'):self.publisher._check()

    def test_browser_repeated_publish_request_is_blocked_and_result_stays_unknown(self):
        self.fill()
        self.publisher.submit()
        self.publisher.read_receipt()
        repeated=self.page.evaluate("""async () => {
            try {await fetch('/pcui/article/publish?type=video', {method:'POST', body:new URLSearchParams({title:document.querySelector('[contenteditable]').innerText})}); return 'accepted'}
            catch {return 'blocked'}
        }""")
        self.assertEqual(repeated,'blocked')
        with self.assertRaises(PlatformFormError):self.publisher.read_receipt()
        self.assertEqual(self.page.evaluate('window.finalClicks'),1)

    def test_visible_fields_do_not_hide_incomplete_upload(self):
        self.page.add_init_script('window.fixtureUploadStalled=true')
        with self.assertRaises(PlatformFormError):
            self.publisher.fill_form(self.page,self.payload,stopped=lambda:False)
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)

    def test_video_thumbnail_cannot_mask_failed_local_cover_decode(self):
        # The real modal also creates fresh blob images for video-frame choices.
        # A failed local cover must not become ready merely because those decode.
        self.page.evaluate('''() => document.querySelector('#dialogs').addEventListener('change', e => {
            const thumbnail=document.createElement('img');
            thumbnail.src=URL.createObjectURL(new Blob(['<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360"/>'], {type:'image/svg+xml'}));
            e.target.closest('[role=dialog]').append(thumbnail);
        })''')
        self.cover.write_bytes(b'\xff\xd8\xffinvalid cover image')
        with self.assertRaises(PlatformFormError):self.fill()
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)

    def test_upload_restart_after_form_fill_blocks_final_click(self):
        self.fill()
        self.page.evaluate("document.querySelector('#cover-tabs-container').insertAdjacentHTML('beforeend','<p>上传中 25%</p>')")
        with self.assertRaises(PlatformFormError):self.publisher.submit()
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)

    def test_publication_text_is_not_treated_as_video_upload_status(self):
        self.payload=PublishPayload(self.video,self.cover,{
            'description':'上传中也能剪辑：胜率提升25%的游戏复盘',
            'topics':'#测试话题','declaration':'无需声明'}, 'fixture-uid',portrait_cover=self.portrait_cover)
        self.fill()
        self.assertEqual(self.page.locator('[contenteditable]').inner_text(),
                         '上传中也能剪辑：胜率提升25%的游戏复盘 #测试话题')
        self.publisher.submit()
        self.assertEqual(self.publisher.read_receipt(),{'platform_work_id':'1862233438702758904'})
        self.assertEqual(self.page.evaluate('window.finalClicks'),1)

    def test_recovered_editor_is_preserved_without_reupload_or_submit(self):
        self.page.add_init_script('window.fixtureRestoreEditor=true')
        with self.assertRaisesRegex(PlatformFormError,'prior video'):
            self.publisher.fill_form(self.page,self.payload,stopped=lambda:False)
        self.assertIsNone(self.page.evaluate('window.selectedVideoSize'))
        self.assertEqual(self.page.locator('[contenteditable]').inner_text(),'default filename')
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)

    def test_matching_unsubmitted_draft_replaces_current_video_without_adding_another(self):
        self.page.add_init_script("window.fixtureRestoreEditor=true;window.fixtureMatchingDraft=true")
        self.publisher.fill_form(self.page,self.payload,stopped=lambda:False,replace_matching_draft=True)
        self.assertEqual(self.page.evaluate('window.selectedVideoSize'),self.video.stat().st_size)
        self.assertIsNone(self.page.evaluate('window.addedVideoSize'))
        self.assertEqual(self.page.locator('[contenteditable]').inner_text(),'确认描述 #测试话题')
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)

    def test_matching_text_without_explicit_prior_attempt_proof_is_preserved(self):
        self.page.add_init_script("window.fixtureRestoreEditor=true;window.fixtureMatchingDraft=true")
        with self.assertRaisesRegex(PlatformFormError,'prior video'):
            self.publisher.fill_form(self.page,self.payload,stopped=lambda:False)
        self.assertIsNone(self.page.evaluate('window.selectedVideoSize'))
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)

    def test_matching_draft_without_new_video_identity_cannot_reach_submit(self):
        self.page.add_init_script("window.fixtureRestoreEditor=true;window.fixtureMatchingDraft=true;window.fixtureKeepOldVideo=true")
        with self.assertRaises(PlatformFormError):
            self.publisher.fill_form(self.page,self.payload,stopped=lambda:False,replace_matching_draft=True)
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)

    def test_new_video_with_empty_covers_can_upload_and_fill_both_covers(self):
        self.page.add_init_script('window.fixtureBlankCovers=true')
        self.publisher.fill_form(self.page,self.payload,stopped=lambda:False)
        self.assertEqual(self.page.locator('[data-testid=select-cover]').count(),0)
        self.assertEqual(self.page.evaluate('window.coverUploadNames'),['cover-upload.png','portrait-cover-upload.png'])
        self.assertEqual(self.page.locator('[data-testid=cover-preview] img').count(),2)
        self.assertEqual(self.page.locator('[contenteditable]').inner_text(),'确认描述 #测试话题')
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)

    def test_topic_confirmation_preserves_literal_text_and_one_final_click(self):
        self.fill()
        self.page.evaluate('window.fixtureConfirmTopics=true')
        self.publisher.submit()
        self.assertEqual(self.page.locator('[role=dialog]:visible').count(),0)
        self.assertEqual(self.publisher.read_receipt(),{'platform_work_id':'1862233438702758904'})
        self.assertEqual(self.page.locator('[contenteditable]').inner_text(),'确认描述 #测试话题')
        self.assertEqual(self.page.evaluate('window.finalClicks'),1)

    def test_changed_text_in_topic_confirmation_is_not_submitted(self):
        self.fill()
        self.page.evaluate('window.fixtureConfirmTopics=true;window.fixtureChangeTopicsText=true')
        with self.assertRaises(PlatformFormError):self.publisher.submit()
        self.assertEqual(self.page.locator('[role=dialog]:visible').count(),1)
        self.assertEqual(self.page.evaluate('window.finalClicks'),1)
