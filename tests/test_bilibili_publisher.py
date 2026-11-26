from contextlib import contextmanager
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock
from support import LoggingStateTestCase

from wt_media_agent.platforms.bilibili.publisher import (
    BilibiliPublisher, BrowserPublisher, PlatformFormError, image_upload_file, validate_submit_body,
)
from wt_media_agent.platforms.publish import PublicationInputError, PublishControl, PublishPayload


class BilibiliPublisherTest(LoggingStateTestCase):
    def setUp(self):
        super().setUp()
        self.content = {'title': '游戏画中画演示', 'description': '演示说明', 'bilibili': {
            'partition_id': 1008, 'partition_name': '游戏', 'tags': ['游戏', '画中画'],
            'statement': '内容含营销信息', 'visibility': 'public', 'rights_reserved': False}}
        self.body = {'title': '游戏画中画演示', 'desc': '演示说明', 'tag': '游戏,画中画',
            'human_type2': 1008, 'copyright': 3, 'no_reprint': 0, 'is_only_self': 0,
            'creation_statement': {'id': 7}, 'recreate': -1,
            'videos': [{'cid': 777, 'filename': 'uploaded-video'}],
            'cover': 'https://i0.hdslb.com/bfs/archive/space.jpg',
            'cover43': 'https://i0.hdslb.com/bfs/archive/main.jpg'}

    def validate(self, body):
        validate_submit_body(body, self.content, cid=777, filename='uploaded-video', statement_id=7, cover_urls={'cover': self.body['cover'], 'cover43': self.body['cover43']})

    def test_cloud_bootstrap_import_does_not_require_playwright(self):
        import subprocess, sys
        script = """
import importlib.abc, sys
class NoPlaywright(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'playwright' or fullname.startswith('playwright.'):
            raise ModuleNotFoundError('local-only browser dependency unavailable')
sys.meta_path.insert(0, NoPlaywright())
import wt_media_agent.cloud_main
import wt_media_agent.bootstrap.app
"""
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_actual_post_must_match_every_confirmed_field_and_one_video(self):
        self.validate(self.body)
        for change in ({'title': 'auto replaced'}, {'desc': ''}, {'tag': '游戏,原创'},
            {'human_type2': 17}, {'copyright': 1}, {'no_reprint': 1}, {'is_only_self': 1},
            {'creation_statement': {'id': -1}}, {'source': 'unexpected'}, {'recreate': 1},
            {'dtime': 123}, {'aid': 999}, {'mission_id': 8}, {'porder': {'id': 7}},
            {'charging_pay': True}, {'cover': 'https://evil.example/cover.jpg'},
            {'videos': [{'cid': 778, 'filename': 'uploaded-video'}]},
            {'videos': self.body['videos'] * 2}, {'cover43': ''},
            {'cover': self.body['cover43'], 'cover43': self.body['cover']},
            {'cover': 'https://i0.hdslb.com/bfs/archive/unrelated.jpg'}):
            with self.subTest(change=change), self.assertRaises(PlatformFormError):
                self.validate({**self.body, **change})

    def test_upload_timeout_counts_normal_browser_check_time(self):
        from unittest.mock import patch
        clock = [0.0]
        adapter = BrowserPublisher(timeout=1)
        adapter.page = MagicMock()
        def check():
            clock[0] += 2
        def wait(_):
            clock[0] += .1
        adapter._check = MagicMock(side_effect=check)
        adapter.page.wait_for_timeout.side_effect = wait
        with patch('wt_media_agent.platforms.bilibili.publisher.time.monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(PlatformFormError):
                adapter._wait(lambda: False)
        self.assertEqual(adapter._check.call_count, 1)

    def test_upload_timeout_excludes_only_explicit_human_wait(self):
        from unittest.mock import patch
        clock = [0.0]
        adapter = BrowserPublisher(timeout=1)
        adapter.page = MagicMock()
        adapter._human_seconds = 0.0
        def check():
            if clock[0] == 0:
                clock[0] += 120
                adapter._human_seconds += 120
        def wait(_):
            clock[0] += .1
        adapter._check = check
        adapter.page.wait_for_timeout.side_effect = wait
        conditions = iter([False, False, True])
        with patch('wt_media_agent.platforms.bilibili.publisher.time.monotonic', side_effect=lambda: clock[0]):
            self.assertTrue(adapter._wait(lambda: next(conditions)))

    def test_upload_proof_uses_original_request_not_response_location(self):
        from urllib.parse import urlencode
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / 'video.mp4'
            video.write_bytes(b'fixed video')
            adapter = BrowserPublisher()
            adapter._payload = MagicMock(video=video)
            response = MagicMock()
            response.url = 'https://member.bilibili.com/preupload'
            response.request.url = response.url + '?' + urlencode({'name': video.name, 'size': video.stat().st_size})
            response.request.method = 'GET'
            response.status = 200
            response.json.return_value = {'biz_id': 777, 'upos_uri': 'upos://current-video.mp4'}
            adapter._response(response)
            self.assertEqual(adapter._cid, 777)
            self.assertEqual(adapter._filename, 'current-video')
            response.request.url = response.url + '?' + urlencode({'name': 'other.mp4', 'size': video.stat().st_size})
            adapter._cid = None
            adapter._seen_upload_responses.clear()
            adapter._response(response)
            self.assertIsNone(adapter._cid)

    def test_upload_transport_diagnostics_do_not_log_private_request_values(self):
        adapter = BrowserPublisher()
        request = MagicMock()
        request.url = 'https://member.bilibili.com/preupload?private=must-not-log'
        request.method = 'GET'
        request.failure = 'net::ERR_FAILED https://private.test/must-not-log'
        with self.assertLogs('wt_media_agent.platforms.bilibili.publisher', level='INFO') as logs:
            adapter._upload_request(request)
            adapter._upload_failed(request)
        self.assertIn('network_error=net::ERR_FAILED', str(logs.output))
        self.assertNotIn('must-not-log', str(logs.output))
        self.assertIsNone(adapter._cid)

    def test_owned_worker_upload_response_reaches_same_frozen_input_validation(self):
        from urllib.parse import urlencode
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / 'video.mp4'
            video.write_bytes(b'fixed video')
            adapter = BrowserPublisher()
            page = MagicMock()
            adapter.protect_scene(page)
            adapter._payload = MagicMock(video=video)
            callbacks = {call.args[0]: call.args[1] for call in page.context.on.call_args_list}
            response = MagicMock()
            response.url = 'https://member.bilibili.com/preupload'
            response.status = 200
            response.request.url = response.url + '?' + urlencode({'name': video.name, 'size': video.stat().st_size})
            response.request.method = 'GET'
            response.request.service_worker.url = 'https://member.bilibili.com/service-worker.js'
            response.json.return_value = {'biz_id': 777, 'upos_uri': 'upos://current-video.mp4'}
            callbacks['response'](response)
            self.assertEqual(adapter._cid, 777)
            adapter._cid = None
            response.request.url = response.url + '?' + urlencode({'name': 'another.mp4', 'size': video.stat().st_size})
            adapter._seen_upload_responses.clear()
            callbacks['response'](response)
            self.assertIsNone(adapter._cid)
            response.request.service_worker.url = 'https://unrelated.test/sw.js'
            callbacks['response'](response)
            self.assertIsNone(adapter._cid)
            # Dedicated/background SDK requests may have no service worker.
            response.request.service_worker = None
            response.request.url = response.url + '?' + urlencode({'name': video.name, 'size': video.stat().st_size})
            adapter._seen_upload_responses.clear()
            callbacks['response'](response)
            self.assertEqual(adapter._cid, 777)

    def test_non_success_upload_is_diagnosed_without_accepting_or_logging_url(self):
        from urllib.parse import urlencode
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / 'video.mp4'
            video.write_bytes(b'fixed video')
            adapter = BrowserPublisher()
            adapter._payload = MagicMock(video=video)
            response = MagicMock()
            response.status = 412
            response.url = 'https://member.bilibili.com/preupload'
            response.request.url = response.url + '?' + urlencode({'name': video.name, 'size': video.stat().st_size, 'private': 'must-not-log'})
            with self.assertLogs('wt_media_agent.platforms.bilibili.publisher', level='INFO') as logs:
                adapter._response(response)
            self.assertIsNone(adapter._cid)
            response.json.assert_not_called()
            self.assertIn('http_status=412 current_video=True', str(logs.output))
            self.assertNotIn('must-not-log', str(logs.output))

    def test_closed_response_never_supplies_upload_or_submission_proof(self):
        from playwright.sync_api import Error as PlaywrightError
        adapter = BrowserPublisher()
        response = MagicMock()
        response.url = 'https://member.bilibili.com/x/vupre/web/archive/pre'
        response.status = 200
        response.json.side_effect = PlaywrightError('private transport detail')
        with self.assertLogs('wt_media_agent.platforms.bilibili.publisher', level='WARNING') as logs:
            adapter._response(response)
        self.assertIsNone(adapter._pre)
        self.assertIsNone(adapter._cid)
        self.assertEqual(adapter._capture['forwarded'], 0)
        self.assertNotIn('private transport detail', str(logs.output))

    def test_guard_blocks_early_duplicate_and_changed_post(self):
        adapter = BrowserPublisher()
        page = MagicMock()
        adapter.protect_scene(page)
        guard = page.route.call_args.args[1]
        adapter._payload = MagicMock(content=self.content)
        adapter._cid, adapter._filename, adapter._statement_id = 777, 'uploaded-video', 7
        adapter._cover_urls = {k: self.body[k] for k in ('cover', 'cover43')}
        import json
        def request(body=None, path='/x/vu/web/add/v3'):
            route = MagicMock()
            route.request.method = 'POST'
            route.request.url = 'https://member.bilibili.com' + path
            route.request.post_data = json.dumps(body or self.body)
            return route
        early = request()
        guard(early)
        early.abort.assert_called_once()
        adapter._submitted = True
        second = request()
        guard(second)
        second.abort.assert_called_once()
        self.assertEqual(adapter._capture['forwarded'], 0)
        fresh = BrowserPublisher()
        fresh.protect_scene(MagicMock())
        fresh._payload, fresh._cid, fresh._filename, fresh._statement_id = adapter._payload, 777, 'uploaded-video', 7
        fresh._cover_urls = adapter._cover_urls
        fresh._submitted = True
        guard = fresh._protected.route.call_args.args[1]
        only = request()
        guard(only)
        only.fallback.assert_called_once()
        duplicate = request()
        guard(duplicate)
        duplicate.abort.assert_called_once()
        for path in ('/x/vu/web/edit', '/x/vupre/web/draft/add', '/x/vupre/web/draft/update'):
            route = request(path=path)
            guard(route)
            route.abort.assert_called_once()

    def test_wrapper_never_submits_after_stop_or_wrong_live_uid(self):
        payload = PublishPayload(Path('/video'), Path('/cover'), self.content, '456', space_cover=Path('/space'))
        calls = []
        implementation = MagicMock()
        @contextmanager
        def session(_):
            yield MagicMock()
        control = PublishControl(lambda: False, lambda: calls.append('prepared'), lambda: calls.append('permit'))
        publisher = BilibiliPublisher('localhost:1', browser_session_factory=session,
            identity_reader=lambda _: {'login_status': 'normal', 'platform_account_id': '999'},
            browser_factory=lambda: implementation)
        with self.assertRaises(PublicationInputError): publisher.publish(payload, control)
        self.assertEqual(calls, [])
        implementation.submit.assert_not_called()
        stopped = False
        def permit():
            nonlocal stopped
            stopped = True
        publisher = BilibiliPublisher('localhost:1', browser_session_factory=session,
            identity_reader=lambda _: {'login_status': 'normal', 'platform_account_id': '456'},
            browser_factory=lambda: implementation)
        with self.assertRaises(PublicationInputError):
            publisher.publish(payload, PublishControl(lambda: stopped, lambda: None, permit))
        implementation.submit.assert_not_called()
        with self.assertRaises(PublicationInputError): publisher.publish(payload, control)

    def test_bilibili_upload_copy_preserves_bytes_and_rejects_webp(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'cover'
            source.write_bytes(b'\x89PNG\r\n\x1a\nimage')
            result = image_upload_file(source)
            self.assertEqual(result.suffix, '.png')
            self.assertEqual(source.read_bytes(), result.read_bytes())
            self.assertEqual(result.stat().st_mode & 0o777, 0o600)
            source.write_bytes(b'RIFF0000WEBPimage')
            with self.assertRaises(PlatformFormError): image_upload_file(source)

    def test_context_cover_response_is_bound_to_its_confirmed_export(self):
        adapter = BrowserPublisher()
        response = MagicMock()
        response.url='https://member.bilibili.com/x/vu/web/cover/up'
        response.status=200
        response.json.return_value={'code':0,'data':{'url':self.body['cover43']}}
        adapter._cover_requests[response.request]='cover43'
        adapter._context_upload_response(response)
        self.assertEqual(adapter._cover_urls, {'cover43':self.body['cover43']})
        other=MagicMock()
        other.url=response.url
        other.status=200
        other.json.return_value={'code':0,'data':{'url':self.body['cover']}}
        adapter._context_upload_response(other)
        self.assertEqual(adapter._cover_urls, {'cover43':self.body['cover43']})

    def test_cover_upload_failure_removes_only_its_temporary_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'cover'
            source.write_bytes(b'\x89PNG\r\n\x1a\nimage')
            adapter = BrowserPublisher()
            adapter.page = MagicMock()
            dialog = adapter.page.locator.return_value
            dialog.evaluate.return_value = {'known': False}
            dialog.locator.return_value.count.return_value = 1
            dialog.locator.return_value.is_checked.return_value = False
            dialog.locator.return_value.set_input_files.side_effect = OSError('upload failed')
            adapter._check = lambda: None
            adapter._cover_canvas = lambda _: None
            payload = PublishPayload(Path('/video'), source, self.content, '456', space_cover=source)
            with self.assertRaises(OSError): adapter._fill_covers(payload)
            self.assertTrue(source.exists())
            self.assertEqual(list(Path(directory).glob('*-upload.*')), [])


if __name__ == '__main__': unittest.main()
