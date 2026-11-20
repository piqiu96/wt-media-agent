"""Real Chrome/Playwright controls against isolated HTML, never platform acceptance."""
import base64
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

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
        self.video=self.folder/'video.mp4'
        self.video.write_bytes(b'fixture video; not a real platform upload')
        self.payload=PublishPayload(self.video,self.cover,{'description':'确认描述','topics':'#测试话题','declaration':'无需声明'}, 'fixture-uid')
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

    def test_full_navigation_upload_and_fill_do_not_submit(self):
        self.publisher.fill_form(self.page,self.payload,stopped=lambda:False)
        self.assertEqual(self.page.evaluate('window.selectedVideoSize'),self.video.stat().st_size)
        self.assertEqual(self.page.locator('[contenteditable]').inner_text(),'确认描述 #测试话题')
        self.assertEqual(self.page.evaluate('window.finalClicks'),0)

    def collapsed_home(self):
        self.home='''<meta charset="utf-8"><div id="home-publish-btn" onmouseenter="window.menuShown=true;document.querySelector('a').style.opacity=1;document.querySelector('a').style.pointerEvents='auto'">＋</div><a style="opacity:0;pointer-events:none" href="/builder/rc/edit">发布视频</a>'''

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
            'topics':'#测试话题','declaration':'无需声明'}, 'fixture-uid')
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
