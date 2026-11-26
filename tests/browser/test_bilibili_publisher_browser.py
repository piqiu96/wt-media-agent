"""Isolated real-browser adapter checks; no requests reach Bilibili."""
import json
import struct
import zlib
from pathlib import Path
import tempfile
import subprocess
import unittest

import test_baidu_publisher_browser as browser_harness
from wt_media_agent.platforms.bilibili.publisher import BrowserPublisher, PlatformFormError
from wt_media_agent.platforms.publish import ExistingPublicationDraft, PublishPayload
from wt_media_agent.services.browser.session import BrowserSession
from wt_media_agent.platforms.bilibili.publication_check import BilibiliPublicationChecker
from wt_media_agent.clients.bilibili.cover import cover_upload_kind
from wt_media_agent.platforms.publication_check import PublicationReference

FIXTURE = Path(__file__).with_name('bilibili-editor.html').read_text()

class FastPublisher(BrowserPublisher):
    def _wait(self, condition, *, timeout=None):
        return super()._wait(condition, timeout=min(timeout or 2, 2))


class BilibiliPublisherBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): browser_harness.BrowserPublisherBrowserTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls): browser_harness.BrowserPublisherBrowserTests.tearDownClass.__func__(cls)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        cover = folder / 'cover'
        def chunk(kind, data):
            return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
        cover.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 4, 3, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\0' + b'\xff\xff\xff' * 4) * 3)) + chunk(b'IEND', b''))
        space = folder / 'space-cover'
        space.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 16, 9, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\0' + b'\0\0\xff' * 16) * 9)) + chunk(b'IEND', b''))
        video = folder / 'video.mp4'
        video.write_bytes(b'isolated fixture video')
        self.payload = PublishPayload(video, cover, {'title': '游戏画中画演示', 'description': '演示说明',
            'bilibili': {'partition_id': 1008, 'partition_name': '游戏', 'tags': ['游戏', '画中画'],
                'statement': '内容含营销信息', 'visibility': 'public', 'rights_reserved': False}}, '456', space_cover=space)
        self.session = BrowserSession(self.endpoint).__enter__()
        self.addCleanup(self.session.close)
        self.page = self.session.page
        self.addCleanup(self.page.close)
        self.page_errors = []
        self.page.on('pageerror', lambda error: self.page_errors.append(str(error)))
        self.sent = 0
        self.unexpected = []
        def isolated(route):
            url = route.request.url
            if url == 'https://member.bilibili.com/platform/upload/video/frame':
                route.fulfill(content_type='text/html', body=FIXTURE)
            elif '/x/vupre/web/archive/pre' in url:
                route.fulfill(json={'code': 0, 'data': {'neutral_mark': {'mark_list': [{'id': 7, 'content': '内容含营销信息'}]}}})
            elif '/x/vupre/web/archive/human/type2/list' in url:
                route.fulfill(json={'code': 0, 'data': {'type_list': [{'id': 1008, 'name': '游戏'}]}})
            elif '/preupload?' in url:
                route.fulfill(json={'biz_id': 777, 'upos_uri': 'upos://fixture-upload.mp4'})
            elif url == 'https://member.bilibili.com/x/vu/web/add/v3':
                self.sent += 1
                route.fulfill(json={'code': 0, 'data': {'aid': 123, 'bvid': 'BVfixture'}})
            elif url == 'https://member.bilibili.com/x/vu/web/cover/up':
                kind = cover_upload_kind(route.request.header_value('content-type'), route.request.post_data_buffer)
                route.fulfill(json={'code': 0, 'data': {'url': 'https://i0.hdslb.com/bfs/archive/' + kind + '.jpg'}})
            else:
                self.unexpected.append(url)
                route.abort()
        self.page.route('**/*', isolated)
        self.adapter = FastPublisher(timeout=2)

    def fill(self):
        try:
            self.adapter.fill_form(self.page, self.payload, stopped=lambda: False)
            self.assertEqual(list(Path(self.temp.name).glob('*-upload.*')), [])
        except Exception:
            if self.page_errors:
                raise AssertionError('isolated editor errors: ' + repr(self.page_errors)) from None
            state = self.page.evaluate("({slot, pixels:[...document.querySelectorAll('canvas.lower-canvas')].map(c=>[...c.getContext('2d').getImageData(0,0,1,1).data])})")
            raise AssertionError('isolated cover state: ' + repr(state))

    def test_service_worker_upload_events_belong_to_context_not_page(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        worker = """
self.addEventListener('activate',e=>e.waitUntil(clients.claim()));
self.addEventListener('message',e=>e.waitUntil(fetch('/preupload?name=video.mp4&size=22')
  .then(r=>r.json()).then(data=>e.source.postMessage(data))));
"""
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                body = worker if self.path == '/sw.js' else ('{"biz_id":777}' if self.path.startswith('/preupload?') else '<html>isolated worker</html>')
                self.send_response(200)
                self.send_header('Content-Type', 'application/javascript' if self.path == '/sw.js' else 'text/html')
                self.end_headers()
                self.wfile.write(body.encode())
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.page.unroute('**/*')
            page_responses, context_responses = [], []
            self.page.on('response', lambda r: page_responses.append(r) if '/preupload?' in r.url else None)
            self.page.context.on('response', lambda r: context_responses.append(r) if '/preupload?' in r.url else None)
            self.page.goto('http://127.0.0.1:' + str(server.server_port))
            result = self.page.evaluate("""async () => {
              await navigator.serviceWorker.register('/sw.js');
              await navigator.serviceWorker.ready;
              if(!navigator.serviceWorker.controller)await new Promise(resolve=>navigator.serviceWorker.addEventListener('controllerchange',resolve,{once:true}));
              return await new Promise(resolve=>{
                navigator.serviceWorker.addEventListener('message',e=>resolve(e.data),{once:true});
                navigator.serviceWorker.controller.postMessage('upload');
              });
            }""")
            self.assertEqual(result, {'biz_id': 777})
            self.assertEqual(page_responses, [])
            self.assertEqual(len(context_responses), 1)
            self.assertIsNotNone(context_responses[0].request.service_worker)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_current_selected_file_binds_completed_video_without_network_receipt(self):
        self.page.add_init_script('window.fixtureNoUploadReceipt=true')
        self.fill()
        self.adapter.submit()
        self.assertEqual(self.adapter.read_receipt(), {'platform_work_id': '123'})
        self.assertEqual(self.sent, 1)

    def test_other_file_object_with_same_bytes_does_not_replace_owned_input(self):
        self.page.add_init_script('window.fixtureNoUploadReceipt=true;window.fixtureDifferentFile=true')
        with self.assertRaises(PlatformFormError):
            self.adapter.fill_form(self.page, self.payload, stopped=lambda:False)
        self.assertEqual(self.sent, 0)

    def test_completed_editor_and_network_upload_ids_must_agree(self):
        self.page.add_init_script('window.fixtureDifferentCid=true')
        with self.assertRaises(PlatformFormError):
            self.adapter.fill_form(self.page, self.payload, stopped=lambda:False)
        self.assertEqual(self.sent, 0)

    def test_current_file_binding_is_rechecked_before_final_submit(self):
        self.page.add_init_script('window.fixtureNoUploadReceipt=true')
        self.fill()
        self.page.locator('.file-list').evaluate("""root=>{
          const row=root.__vue__.videoTasks[0];row.file=new File([row.file],row.file.name);
        }""")
        with self.assertRaises(PlatformFormError): self.adapter.submit()
        self.assertEqual(self.sent, 0)

    def test_multiple_completed_videos_cannot_supply_one_input_proof(self):
        self.page.add_init_script('window.fixtureNoUploadReceipt=true;window.fixtureMultipleVideos=true')
        with self.assertRaises(PlatformFormError):
            self.adapter.fill_form(self.page, self.payload, stopped=lambda:False)
        self.assertEqual(self.sent, 0)

    def test_recovery_banner_does_not_block_fresh_authorized_upload(self):
        self.page.route('https://member.bilibili.com/platform/upload/video/frame',
            lambda route: route.fulfill(content_type='text/html', body=FIXTURE.replace(
                '<meta charset="utf-8">', '<meta charset="utf-8"><button onclick="window.recoveryClicked=true">继续编辑</button><button onclick="window.discardClicked=true">不用了</button>')))
        self.fill()
        self.assertIsNone(self.page.evaluate('window.recoveryClicked'))
        self.assertIsNone(self.page.evaluate('window.discardClicked'))
        self.assertEqual(self.page.locator('#video-file').evaluate('(e)=>e.files[0].size'), self.payload.video.stat().st_size)
        self.assertEqual(self.sent, 0)
        self.adapter.submit()
        self.assertEqual(self.adapter.read_receipt(), {'platform_work_id': '123'})
        self.assertEqual(self.sent, 1)

    def test_active_restored_video_is_not_overwritten(self):
        self.page.route('https://member.bilibili.com/platform/upload/video/frame',
            lambda route: route.fulfill(content_type='text/html', body=FIXTURE.replace(
                '<meta charset="utf-8">', '<meta charset="utf-8"><span>上传完成</span>')))
        with self.assertRaises(ExistingPublicationDraft):
            self.adapter.fill_form(self.page, self.payload, stopped=lambda: False,
                                   replace_matching_draft=True)
        self.assertEqual(self.page.locator('#video-file').evaluate('(e)=>e.files.length'), 0)
        self.assertEqual(self.sent, 0)

    def test_confirmed_description_trailing_newlines_are_preserved(self):
        self.payload.content['description'] = '演示说明\n\n'
        self.fill()
        self.assertEqual(self.adapter._description_text(), '演示说明\n\n')
        self.assertEqual(self.page.evaluate('window.fixture.description'), '演示说明\n\n')
        self.adapter.submit()
        self.assertEqual(self.adapter.read_receipt(), {'platform_work_id': '123'})
        self.assertEqual(self.sent, 1)

    def test_quill_and_browser_blank_blocks_preserve_exact_description_lines(self):
        self.adapter.page = self.page
        for markup in ('演示说明<div><br></div><div><br></div>',
                       '<p>演示说明</p><p><br></p><p><br></p>'):
            self.page.goto('https://member.bilibili.com/platform/upload/video/frame')
            self.page.locator('main').evaluate('(e)=>e.hidden=false')
            self.page.locator('.ql-editor').evaluate('(e,html)=>e.innerHTML=html', markup)
            self.assertEqual(self.adapter._description_text(), '演示说明\n\n')

    def test_removed_confirmed_trailing_newline_blocks_changed_submission(self):
        self.payload.content['description'] = '演示说明\n'
        self.fill()
        self.page.locator('.ql-editor').fill('演示说明')
        with self.assertRaises(PlatformFormError):
            self.adapter.submit()
        self.assertEqual(self.sent, 0)

    def test_full_form_covers_tags_and_one_accepted_receipt(self):
        self.fill()
        self.adapter.submit()
        self.assertEqual(self.adapter.read_receipt(), {'platform_work_id': '123'})
        with self.assertRaises(PlatformFormError): self.adapter.submit()
        self.assertEqual(self.sent, 1)
        self.assertEqual(self.unexpected, [])

    def test_usable_editor_does_not_wait_for_unrelated_document_resource(self):
        # The live creator is a micro-app. A late resource must not prevent
        # an already usable editor from handling the authorized upload.
        pending = []
        self.page.route('**/late-resource.js', lambda route: pending.append(route))
        self.page.route('https://member.bilibili.com/platform/upload/video/frame',
            lambda route: route.fulfill(content_type='text/html',
                body=FIXTURE.replace('<meta charset="utf-8">',
                    '<meta charset="utf-8"><script src="/late-resource.js" defer></script>')))
        self.fill()
        self.adapter.submit()
        self.assertEqual(self.adapter.read_receipt(), {'platform_work_id': '123'})
        self.assertNotEqual(self.page.evaluate('document.readyState'), 'complete')
        self.assertEqual(self.sent, 1)
        self.assertEqual(len(pending), 1)
        pending[0].fulfill(content_type='application/javascript', body='')

    def test_changed_form_after_permission_never_sends(self):
        self.fill()
        self.page.get_by_placeholder('请输入稿件标题', exact=True).fill('changed')
        with self.assertRaises(PlatformFormError): self.adapter.submit()
        self.assertEqual(self.sent, 0)

    def test_changed_final_post_is_blocked_even_if_dom_was_verified(self):
        self.fill()
        self.page.evaluate('window.fixture.mutatePost=true')
        self.adapter.submit()
        self.page.wait_for_timeout(100)
        self.assertEqual(self.sent, 0)
        self.assertEqual(self.adapter._capture['forwarded'], 0)

    def test_changed_independent_space_cover_blocks_submission(self):
        self.fill()
        self.page.evaluate("document.querySelector('#editor_16_9 canvas').getContext('2d').fillRect(0,0,30,30)")
        with self.assertRaises(PlatformFormError): self.adapter.submit()
        self.assertEqual(self.sent, 0)

    def test_swapped_final_cover_urls_are_blocked(self):
        self.fill()
        self.page.evaluate('window.fixture.swapCovers=true')
        self.adapter.submit()
        self.page.wait_for_timeout(100)
        self.assertEqual(self.sent, 0)

    def test_unrelated_cdn_cover_url_is_blocked(self):
        self.fill()
        self.page.evaluate('window.fixture.staleCover=true')
        self.adapter.submit()
        self.page.wait_for_timeout(100)
        self.assertEqual(self.sent, 0)

    def public_proof(self, *, anonymous_public):
        video = Path(self.temp.name) / 'proof.mp4'
        encoder = Path('/opt/homebrew/bin/ffmpeg')
        if not encoder.is_file(): raise RuntimeError('FFmpeg is required for actual browser media proof tests')
        subprocess.run([str(encoder), '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
            'color=c=blue:s=32x32:d=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', 'faststart', str(video)], check=True)
        metadata = {'aid': 123, 'bvid': 'BV135WM6uE4A', 'owner': {'mid': 456}, 'title': '游戏画中画演示',
                    'state': 0, 'cid': 777, 'pubdate': 1795169573, 'duration': 1}
        observed = []
        media_url = 'https://www.bilibili.com/fixture/proof.mp4'
        def isolated(route):
            request = route.request
            authenticated = 'fixture_session=private' in request.all_headers().get('cookie', '')
            observed.append(authenticated)
            if not anonymous_public and not authenticated:
                route.fulfill(status=403, body='Login required')
            elif request.url == 'https://api.bilibili.com/x/web-interface/view?aid=123':
                route.fulfill(json={'code': 0, 'data': metadata})
            elif request.url == 'https://www.bilibili.com/video/av123':
                route.fulfill(content_type='text/html', body='<meta charset="utf-8"><script>window.__INITIAL_STATE__={videoData:'
                    + json.dumps(metadata, ensure_ascii=False) + '}</script><video autoplay muted preload="auto" src="' + media_url + '"></video>')
            elif request.url == media_url:
                route.fulfill(content_type='video/mp4', body=video.read_bytes())
            else:
                route.abort()
        browser = self.page.context.browser
        self.page.context.add_cookies([{'name': 'fixture_session', 'value': 'private', 'domain': '.bilibili.com', 'path': '/', 'secure': True}])
        original = browser.new_context
        def context(**kwargs):
            self.assertEqual(kwargs['storage_state'], {'cookies': [], 'origins': []})
            created = original(**kwargs)
            created.route('**/*', isolated)
            return created
        browser.new_context = context
        try:
            result = BilibiliPublicationChecker()._anonymous_proof(browser,
                PublicationReference('456', '123', self.payload.content))
        finally:
            browser.new_context = original
        self.assertTrue(observed)
        self.assertFalse(any(observed))
        return result

    def test_anonymous_loaded_original_media_establishes_public_proof(self):
        self.assertIsNotNone(self.public_proof(anonymous_public=True))

    def test_logged_in_only_media_is_not_public_proof(self):
        self.assertIsNone(self.public_proof(anonymous_public=False))


if __name__ == '__main__': unittest.main()
