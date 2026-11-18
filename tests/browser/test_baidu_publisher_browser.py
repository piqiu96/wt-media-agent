"""Real Chrome/Playwright controls against isolated HTML, never platform acceptance."""
import base64
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from wt_media_agent.clients.baidu.publisher import BaiduPublisher, PlatformFormError
from wt_media_agent.clients.publisher import PublishPayload
from wt_media_agent.clients.browser import BrowserSession

CHROME = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
FIXTURE = Path(__file__).with_name('baidu-editor.html').read_text()


class BaiduPublisherBrowserTests(unittest.TestCase):
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
        self.video=self.folder/'video.mp4'
        self.video.write_bytes(b'fixture video; not a real platform upload')
        self.payload=PublishPayload(self.video,self.cover,{'description':'确认描述','topics':'#测试话题','declaration':'无需声明'})
        self.session=BrowserSession(self.endpoint).__enter__()
        self.addCleanup(self.session.close)
        self.page=self.session.page
        self.external=[]
        def isolated(route):
            request=route.request
            if request.url=='https://baijiahao.baidu.com/builder/rc/home':
                route.fulfill(content_type='text/html',body='<meta charset="utf-8"><a href="/builder/rc/edit">发布视频</a>')
            elif request.url=='https://baijiahao.baidu.com/builder/rc/edit':
                route.fulfill(content_type='text/html',body=FIXTURE)
            elif request.url=='https://baijiahao.baidu.com/pcui/article/publish?type=video' and request.method=='POST':
                route.fulfill(content_type='application/json',body='{"errno":0,"ret":{"article_id":1862233438702758904}}')
            else:
                self.external.append(request.url)
                route.abort()
        self.page.route('**/*',isolated)
        self.page.goto('https://baijiahao.baidu.com/builder/rc/edit')
        self.page.evaluate('window.fixtureEditor()')
        self.publisher=BaiduPublisher(timeout=.5)

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
