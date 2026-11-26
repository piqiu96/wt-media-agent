"""Isolated real-browser adapter checks; no requests reach Bilibili."""
import json
import struct
import zlib
from pathlib import Path
import tempfile
import subprocess
import shutil
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
            # This localhost worker belongs only to this fixture. Leaving it
            # registered in the shared test Chrome can stall later CDP routing
            # updates after the fixture server has gone away.
            if not self.page.is_closed():
                self.page.evaluate("async()=>{const registrations=await navigator.serviceWorker.getRegistrations();await Promise.all(registrations.map(r=>r.unregister()));}")
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

    def _reject_unbound_network_receipt(self, flag):
        self.page.add_init_script('window.'+flag+'=true')
        with self.assertRaises(PlatformFormError):
            self.adapter.fill_form(self.page, self.payload, stopped=lambda:False)
        self.assertEqual(self.sent, 0)

    def test_network_receipt_does_not_accept_another_file_object(self):
        self._reject_unbound_network_receipt('fixtureDifferentFile')

    def test_network_receipt_does_not_accept_multiple_video_rows(self):
        self._reject_unbound_network_receipt('fixtureMultipleVideos')

    def test_network_receipt_does_not_accept_incomplete_current_video(self):
        self._reject_unbound_network_receipt('fixtureIncompleteUpload')

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

    def test_saved_cover_check_does_not_reopen_rendering_canvases(self):
        html=FIXTURE+"""<script>
        const open=document.querySelector('.cover-module-main').onclick;
        window.fixture.openCount=0;
        document.querySelector('.cover-module-main').onclick=()=>{
          window.fixture.openCount++;open();if(!window.fixture.cover43)return;
          const canvases=[...document.querySelectorAll('canvas.lower-canvas')];
          const images=canvases.map(c=>c.getContext('2d').getImageData(0,0,c.width,c.height));
          canvases.forEach(c=>c.getContext('2d').clearRect(0,0,c.width,c.height));
          setTimeout(()=>canvases.forEach((c,i)=>c.getContext('2d').putImageData(images[i],0,0)),150);
        };
        </script>"""
        self.page.route('https://member.bilibili.com/platform/upload/video/frame',
                        lambda route:route.fulfill(content_type='text/html',body=html))
        self.fill()
        self.adapter.submit()
        self.assertEqual(self.adapter.read_receipt(), {'platform_work_id':'123'})
        self.assertEqual(self.sent, 1)
        self.assertEqual(self.page.evaluate('window.fixture.openCount'), 1)

    def test_current_cover_slot_uses_css_background_and_waits_for_preview(self):
        html=FIXTURE.replace('<img id="preview">', '<div id="preview" class="cover-img" style="width:200px;height:150px"></div>')
        html=html.replace("$('#preview').src=$('#editor_4_3 canvas').toDataURL()",
            "setTimeout(()=>{$('#preview').style.backgroundImage='url('+$('#editor_4_3 canvas').toDataURL()+')';},150)")
        self.page.route('https://member.bilibili.com/platform/upload/video/frame',
                        lambda route:route.fulfill(content_type='text/html',body=html))
        self.fill()
        self.adapter.submit()
        self.assertEqual(self.adapter.read_receipt(), {'platform_work_id':'123'})
        self.assertEqual(self.sent, 1)

    def test_changed_css_cover_preview_is_not_submitted(self):
        html=FIXTURE.replace('<img id="preview">', '<div id="preview" class="cover-img" style="width:200px;height:150px"></div>')
        html=html.replace("$('#preview').src=$('#editor_4_3 canvas').toDataURL()",
                          "$('#preview').style.backgroundImage='url('+$('#editor_4_3 canvas').toDataURL()+')'")
        self.page.route('https://member.bilibili.com/platform/upload/video/frame',
                        lambda route:route.fulfill(content_type='text/html',body=html))
        self.fill()
        self.page.locator('#preview').evaluate("e=>e.style.backgroundImage='url('+document.querySelector('#editor_16_9 canvas').toDataURL()+')'")
        with self.assertRaises(PlatformFormError): self.adapter.submit()
        self.assertEqual(self.sent, 0)

    def test_official_export_receipt_urls_survive_the_final_post(self):
        def cover_response(route):
            kind=cover_upload_kind(route.request.header_value('content-type'), route.request.post_data_buffer)
            route.fulfill(json={'code':'0','data':{'url':'//archive.biliimg.com/bfs/archive/'+kind+'.jpg'}})
        self.page.route('https://member.bilibili.com/x/vu/web/cover/up', cover_response)
        self.fill()
        self.adapter.submit()
        self.assertEqual(self.adapter.read_receipt(), {'platform_work_id':'123'})
        self.assertEqual(self.sent, 1)

    def test_dual_cover_sync_uses_component_state_with_two_ratio_controls(self):
        html = FIXTURE.replace('<label class="sync-checkbox"><input type="checkbox" checked>双比例同步改动</label>',
            '<div class="ratio_4_3"><button class="sync-checkbox">双比例同步改动</button></div><div class="ratio_16_9"><button class="sync-checkbox">双比例同步改动</button></div>')
        html += """<script>
          const editor=document.querySelector('.cover-editor');
          editor.__vue__={isSyncEnabled:true};
          editor.querySelectorAll('.sync-checkbox').forEach(e=>e.onclick=()=>editor.__vue__.isSyncEnabled=!editor.__vue__.isSyncEnabled);
        </script>"""
        self.page.route('https://member.bilibili.com/platform/upload/video/frame',
                        lambda route: route.fulfill(content_type='text/html', body=html))
        self.fill()
        self.assertFalse(self.page.locator('.cover-editor').evaluate('(e)=>e.__vue__.isSyncEnabled'))
        self.adapter.submit()
        self.assertEqual(self.adapter.read_receipt(), {'platform_work_id':'123'})
        self.assertEqual(self.sent, 1)

    def test_recreate_component_with_multiple_internal_inputs(self):
        html = FIXTURE.replace('<div class="video-recreate"><label', '<div class="video-recreate"><input type="checkbox" hidden><label')
        html += """<script>
        const component=document.querySelector('.video-recreate');
        const toggle=component.querySelector('.bcc-checkbox input');
        component.__vue__={_recreate:true};
        toggle.onchange=()=>component.__vue__._recreate=toggle.checked;
        </script>"""
        self.page.route('https://member.bilibili.com/platform/upload/video/frame',
                        lambda route: route.fulfill(content_type='text/html', body=html))
        self.fill()
        self.assertFalse(self.page.locator('.video-recreate').evaluate('(e)=>e.__vue__._recreate'))

    def test_unknown_duplicate_recreate_controls_do_not_grant_rights(self):
        html = FIXTURE.replace('<div class="video-recreate"><label', '<div class="video-recreate"><input type="checkbox" hidden><label')
        self.page.route('https://member.bilibili.com/platform/upload/video/frame',
                        lambda route: route.fulfill(content_type='text/html', body=html))
        with self.assertRaises(PlatformFormError):
            self.adapter.fill_form(self.page, self.payload, stopped=lambda:False)
        self.assertEqual(self.sent, 0)

    def test_initializing_upload_input_does_not_lose_video_selection(self):
        html=FIXTURE+"""<script>
        const input=document.querySelector('#video-file');
        const choose=input.onchange;input.disabled=true;
        input.onchange=e=>{if(input.disabled)return;window.fixture.videoSelections=(window.fixture.videoSelections||0)+1;return choose(e);};
        setTimeout(()=>input.disabled=false,750);
        </script>"""
        self.page.route('https://member.bilibili.com/platform/upload/video/frame',
                        lambda route:route.fulfill(content_type='text/html',body=html))
        self.fill()
        self.assertEqual(self.page.evaluate('window.fixture.videoSelections'), 1)
        self.adapter.submit()
        self.assertEqual(self.adapter.read_receipt(), {'platform_work_id':'123'})
        self.assertEqual(self.sent, 1)

    def test_async_tag_validation_preserves_every_confirmed_tag(self):
        html=FIXTURE+"""<script>
        const input=document.querySelector('#tag-container input');
        const add=input.onkeydown;let checking=false;
        input.onkeydown=e=>{
          if(e.key!=='Enter'||checking)return;
          const value=input.value;checking=true;
          setTimeout(()=>{input.value=value;add(e);checking=false;},180);
        };
        </script>"""
        self.page.route('https://member.bilibili.com/platform/upload/video/frame',
                        lambda route:route.fulfill(content_type='text/html',body=html))
        self.payload.content['bilibili']['tags']=['游戏','画中画','三角洲行动','航天','激光房','闪击']
        self.fill()
        self.assertEqual(self.page.locator('#tag-container .label-item-v2-content').all_text_contents(),
                         self.payload.content['bilibili']['tags'])
        self.adapter.submit()
        self.assertEqual(self.adapter.read_receipt(), {'platform_work_id':'123'})
        self.assertEqual(self.sent, 1)

    def test_tag_validation_busy_after_visible_acceptance_is_respected(self):
        html=FIXTURE+"""<script>
        const input=document.querySelector('#tag-container input');
        const add=input.onkeydown;
        const vm={checkCustomTagLoading:false};
        document.querySelector('#tag-container').__vue__=vm;
        input.onkeydown=e=>{
          if(e.key!=='Enter'||vm.checkCustomTagLoading)return;
          const value=input.value;vm.checkCustomTagLoading=true;
          setTimeout(()=>{input.value=value;add(e);
            setTimeout(()=>vm.checkCustomTagLoading=false,250);},100);
        };
        </script>"""
        self.page.route('https://member.bilibili.com/platform/upload/video/frame',
                        lambda route:route.fulfill(content_type='text/html',body=html))
        self.fill()
        self.adapter.submit()
        self.assertEqual(self.adapter.read_receipt(), {'platform_work_id':'123'})
        self.assertEqual(self.sent, 1)

    def test_tag_input_is_scoped_to_current_editable_control(self):
        html = FIXTURE.replace('<main hidden>', '<div id="other-tags" hidden><input placeholder="按回车键Enter创建标签"><div class="tag-pre-wrp"><span class="label-item-v2-content">其他表单</span></div></div><main hidden>')
        html = html.replace('<div class="tag-input-wrp">', '<div class="tag-input-wrp" placeholder="按回车键Enter创建标签">', 1)
        self.page.route('https://member.bilibili.com/platform/upload/video/frame',
            lambda route: route.fulfill(content_type='text/html', body=html))
        self.fill()
        self.assertEqual(self.page.locator('#other-tags input').input_value(), '')
        self.assertEqual(self.page.locator('#other-tags .label-item-v2-content').text_content(), '其他表单')
        self.adapter.submit()
        self.assertEqual(self.adapter.read_receipt(), {'platform_work_id':'123'})
        self.assertEqual(self.sent, 1)

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
        self.page.get_by_placeholder('请输入稿件标题', exact=True).fill('private-changed-title')
        with self.assertLogs('wt_media_agent.platforms.bilibili.publisher', level='WARNING') as logs:
            with self.assertRaises(PlatformFormError): self.adapter.submit()
        self.assertIn('fields=title', ' '.join(logs.output))
        self.assertNotIn('private-changed-title', ' '.join(logs.output))
        self.assertEqual(self.sent, 0)

    def test_changed_final_post_is_blocked_even_if_dom_was_verified(self):
        self.fill()
        self.page.evaluate('window.fixture.mutatePost=true')
        self.adapter.submit()
        self.page.wait_for_timeout(100)
        self.assertEqual(self.sent, 0)
        self.assertEqual(self.adapter._capture['forwarded'], 0)

    def test_unapplied_hidden_canvas_does_not_replace_confirmed_cover_urls(self):
        self.fill()
        confirmed=dict(self.adapter._cover_urls)
        self.page.evaluate("document.querySelector('#editor_16_9 canvas').getContext('2d').fillRect(0,0,30,30)")
        self.adapter.submit()
        self.assertEqual(self.adapter.read_receipt(), {'platform_work_id':'123'})
        body=self.adapter._request.post_data_json
        self.assertEqual({key:body[key] for key in ('cover','cover43')}, confirmed)
        self.assertEqual(self.sent, 1)

    def test_overwriting_main_canvas_during_space_upload_is_not_saved(self):
        html=FIXTURE.replace("c.getContext('2d').drawImage(img,0,0,c.width,c.height);",
            "c.getContext('2d').drawImage(img,0,0,c.width,c.height);if(slot==='#editor_16_9'){const main=$('#editor_4_3 canvas');main.getContext('2d').drawImage(img,0,0,main.width,main.height);}")
        self.page.route('https://member.bilibili.com/platform/upload/video/frame',
                        lambda route:route.fulfill(content_type='text/html',body=html))
        with self.assertRaises(PlatformFormError):
            self.adapter.fill_form(self.page,self.payload,stopped=lambda:False)
        self.assertEqual(self.sent, 0)
        self.assertEqual(self.adapter._cover_urls, {})

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

    def public_proof(self, *, anonymous_public, media_matches=True, playback_cid=777, use_blob=False, requires_play=False):
        video = Path(self.temp.name) / 'proof.mp4'
        encoder = shutil.which('ffmpeg')
        if encoder is None: raise RuntimeError('FFmpeg is required for actual browser media proof tests')
        subprocess.run([encoder, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
            'color=c=blue:s=32x32:d=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', 'faststart', str(video)], check=True)
        metadata = {'aid': 123, 'bvid': 'BV135WM6uE4A', 'owner': {'mid': 456}, 'title': '游戏画中画演示',
                    'state': 0, 'cid': 777, 'pubdate': 1795169573, 'duration': 1}
        observed = []
        media_url = 'https://www.bilibili.com/fixture/proof.mp4'
        selected_url = media_url if media_matches else media_url + '?unrelated=1'
        def isolated(route):
            request = route.request
            authenticated = 'fixture_session=private' in request.all_headers().get('cookie', '')
            observed.append(authenticated)
            if not anonymous_public and not authenticated:
                route.fulfill(status=403, body='Login required')
            elif request.url == 'https://api.bilibili.com/x/web-interface/view?aid=123':
                route.fulfill(json={'code': 0, 'data': metadata})
            elif request.url == 'https://www.bilibili.com/video/av123':
                playback='https://api.bilibili.com/x/player/wbi/playurl?cid='+str(playback_cid)
                media = ('<video autoplay muted preload="auto"></video><script>fetch('+json.dumps(selected_url)+').then(r=>r.blob()).then(b=>document.querySelector("video").src=URL.createObjectURL(b))</script>'
                    if use_blob else '<video autoplay muted preload="auto" src="'+selected_url+'"></video>')
                if requires_play:
                    media = '<video controls preload="none" src="'+selected_url+'"></video><button class="bpx-player-ctrl-play" onclick="document.querySelector(\'video\').play()">播放</button>'
                route.fulfill(content_type='text/html', body='<meta charset="utf-8"><script>window.__INITIAL_STATE__={videoData:'
                    + json.dumps(metadata, ensure_ascii=False) + '};fetch('+json.dumps(playback)+')</script>'+media)
            elif request.url.startswith('https://api.bilibili.com/x/player/wbi/playurl?'):
                route.fulfill(json={'code':0,'data':{'durl':[{'url':media_url}]}})
            elif request.url in {media_url, selected_url}:
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

    def test_anonymous_player_requiring_play_still_proves_original_media(self):
        self.assertIsNotNone(self.public_proof(anonymous_public=True, requires_play=True))

    def test_matching_metadata_with_other_playing_media_is_not_public_proof(self):
        self.assertIsNone(self.public_proof(anonymous_public=True, media_matches=False))

    def test_blob_media_loaded_from_matching_cid_response_is_public_proof(self):
        self.assertIsNotNone(self.public_proof(anonymous_public=True, use_blob=True))

    def test_playback_response_for_other_cid_is_not_public_proof(self):
        self.assertIsNone(self.public_proof(anonymous_public=True, playback_cid=778))

    def test_logged_in_only_media_is_not_public_proof(self):
        self.assertIsNone(self.public_proof(anonymous_public=False))


if __name__ == '__main__': unittest.main()
