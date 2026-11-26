from contextlib import contextmanager
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

from wt_media_agent.platforms.bilibili.publisher import (
    BilibiliPublisher, BrowserPublisher, PlatformFormError, image_upload_file, validate_submit_body,
)
from wt_media_agent.platforms.publish import PublicationInputError, PublishControl, PublishPayload


class BilibiliPublisherTest(unittest.TestCase):
    def setUp(self):
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
        validate_submit_body(body, self.content, cid=777, filename='uploaded-video', statement_id=7)

    def test_actual_post_must_match_every_confirmed_field_and_one_video(self):
        self.validate(self.body)
        for change in ({'title': 'auto replaced'}, {'desc': ''}, {'tag': '游戏,原创'},
            {'human_type2': 17}, {'copyright': 1}, {'no_reprint': 1}, {'is_only_self': 1},
            {'creation_statement': {'id': -1}}, {'source': 'unexpected'}, {'recreate': 1},
            {'dtime': 123}, {'aid': 999}, {'mission_id': 8}, {'porder': {'id': 7}},
            {'charging_pay': True}, {'cover': 'https://evil.example/cover.jpg'},
            {'videos': [{'cid': 778, 'filename': 'uploaded-video'}]},
            {'videos': self.body['videos'] * 2}, {'cover43': ''}):
            with self.subTest(change=change), self.assertRaises(PlatformFormError):
                self.validate({**self.body, **change})

    def test_guard_blocks_early_duplicate_and_changed_post(self):
        adapter = BrowserPublisher()
        page = MagicMock()
        adapter.protect_scene(page)
        guard = page.route.call_args.args[1]
        adapter._payload = MagicMock(content=self.content)
        adapter._cid, adapter._filename, adapter._statement_id = 777, 'uploaded-video', 7
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


if __name__ == '__main__': unittest.main()
