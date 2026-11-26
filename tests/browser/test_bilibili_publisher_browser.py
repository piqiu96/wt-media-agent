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
from wt_media_agent.platforms.publish import PublishPayload
from wt_media_agent.services.browser.session import BrowserSession
from wt_media_agent.platforms.bilibili.publication_check import BilibiliPublicationChecker
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
            else:
                self.unexpected.append(url)
                route.abort()
        self.page.route('**/*', isolated)
        self.adapter = FastPublisher(timeout=2)

    def fill(self):
        try:
            self.adapter.fill_form(self.page, self.payload, stopped=lambda: False)
        except Exception:
            if self.page_errors:
                raise AssertionError('isolated editor errors: ' + repr(self.page_errors)) from None
            state = self.page.evaluate("({slot, pixels:[...document.querySelectorAll('canvas.lower-canvas')].map(c=>[...c.getContext('2d').getImageData(0,0,1,1).data])})")
            raise AssertionError('isolated cover state: ' + repr(state))

    def test_full_form_covers_tags_and_one_accepted_receipt(self):
        self.fill()
        self.adapter.submit()
        self.assertEqual(self.adapter.read_receipt(), {'platform_work_id': '123'})
        with self.assertRaises(PlatformFormError): self.adapter.submit()
        self.assertEqual(self.sent, 1)
        self.assertEqual(self.unexpected, [])

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
