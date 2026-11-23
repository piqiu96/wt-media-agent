"""Standalone publication controls: no Cloud model, DB or real platform access."""
import unittest
from pathlib import Path
from unittest.mock import Mock

from wt_media_agent.platforms.publish import PublishPayload, PublishControl, PublicationInputError
from wt_media_agent.platforms.baidu.publisher import BaiduPublisher
from wt_media_agent.runtime.constants import PublishMethod


class PlatformPublishTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.live = True
        self.payload = PublishPayload(Path('video'), Path('cover'), {'description': 'confirmed'}, 'uid')
        self.backend = Mock()
        self.backend.fill_form.side_effect = lambda *a, **kw: self.events.append('fill')
        self.backend.submit.side_effect = lambda: self.events.append('submit')
        self.backend.read_receipt.side_effect = lambda: (self.events.append('receipt'), {'platform_work_id': '123'})[1]
        events = self.events
        class Session:
            page = object()
            def __enter__(self): events.append('connect'); return self
            def __exit__(self, *_): events.append('disconnect')
        self.session_factory = Mock(return_value=Session())
        self.identity = Mock(side_effect=lambda endpoint: (events.append('identity'), {'login_status': 'normal', 'platform_account_id': 'uid'})[1])
        self.publisher = BaiduPublisher('127.0.0.1:9999', browser_session_factory=self.session_factory,
            identity_reader=self.identity, browser_factory=lambda: self.backend)
        self.control = PublishControl(stopped=lambda: not self.live,
            on_prepared=lambda: events.append('prepared'), before_submit=lambda: events.append('permission'))

    def test_default_browser_path_checks_identity_and_permission_before_one_submit(self):
        self.assertEqual(self.publisher.publish(self.payload, self.control), {'platform_work_id': '123'})
        self.assertEqual(self.events, ['identity', 'prepared', 'connect', 'fill', 'identity', 'permission', 'submit', 'receipt', 'disconnect'])
        with self.assertRaises(PublicationInputError): self.publisher.publish(self.payload, self.control)
        self.assertEqual(self.events.count('submit'), 1)

    def test_login_wait_keeps_owned_session_and_verifies_account_before_upload(self):
        page=Mock()
        self.session_factory.return_value.page=page
        self.identity.side_effect=[{'login_status':'expired'}, {'login_status':'normal','platform_account_id':'uid'}, {'login_status':'normal','platform_account_id':'uid'}]
        def human(show):
            self.events.append('human')
            show()
        control=PublishControl(stopped=lambda:False,on_prepared=lambda:self.events.append('prepared'),before_submit=lambda:self.events.append('permission'),wait_for_human=human)
        self.publisher.publish(self.payload,control)
        self.assertEqual(self.events,['connect','human','prepared','fill','permission','submit','receipt','disconnect'])
        self.assertEqual(self.session_factory.call_count,1)
        self.assertEqual(self.backend.fill_form.call_count,1)
        page.bring_to_front.assert_called_once()
        page.goto.assert_called_once()

    def test_explicit_browser_uses_the_same_implementation(self):
        self.publisher.publish(self.payload, self.control, method=PublishMethod.BROWSER)
        self.assertEqual(self.events.count('submit'), 1)

    def test_unsupported_method_never_reads_identity_or_connects_or_falls_back(self):
        for method in ('api', '', None, [], {}):
            with self.subTest(method=method), self.assertRaises(PublicationInputError):
                self.publisher.publish(self.payload, self.control, method=method)
        self.identity.assert_not_called()
        self.session_factory.assert_not_called()
        self.backend.submit.assert_not_called()

    def test_wrong_initial_uid_cannot_mark_ready_or_connect(self):
        self.identity.return_value = {'login_status': 'normal', 'platform_account_id': 'other'}
        self.identity.side_effect = None
        with self.assertRaises(PublicationInputError): self.publisher.publish(self.payload, self.control)
        self.assertEqual(self.events, [])
        self.session_factory.assert_not_called()

    def test_changed_uid_after_upload_cannot_request_permission(self):
        self.identity.side_effect = [{'login_status': 'normal', 'platform_account_id': 'uid'},
                                     {'login_status': 'normal', 'platform_account_id': 'other'}]
        with self.assertRaises(PublicationInputError): self.publisher.publish(self.payload, self.control)
        self.assertEqual(self.events, ['prepared', 'connect', 'fill', 'disconnect'])

    def test_permission_loss_cannot_submit_and_still_disconnects(self):
        control = PublishControl(stopped=lambda: False, on_prepared=lambda: None,
            before_submit=Mock(side_effect=ConnectionError('lost permission')))
        with self.assertRaises(ConnectionError): self.publisher.publish(self.payload, control)
        self.backend.submit.assert_not_called()
        self.assertEqual(self.events[-1], 'disconnect')

    def test_stop_after_permission_cannot_submit(self):
        def stop(): self.live = False
        control = PublishControl(stopped=lambda: not self.live, on_prepared=lambda: None, before_submit=stop)
        with self.assertRaises(PublicationInputError): self.publisher.publish(self.payload, control)
        self.backend.submit.assert_not_called()
        self.assertEqual(self.events[-1], 'disconnect')

    def test_lost_submit_response_cannot_replay_or_switch_implementation(self):
        self.backend.submit.side_effect = ConnectionError('lost acknowledgement')
        with self.assertRaises(ConnectionError): self.publisher.publish(self.payload, self.control)
        with self.assertRaises(PublicationInputError): self.publisher.publish(self.payload, self.control)
        self.assertEqual(self.backend.submit.call_count, 1)
        self.assertEqual(self.events[-1], 'disconnect')

    def test_missing_account_identity_fails_before_any_operation(self):
        payload = PublishPayload(Path('video'), Path('cover'), {}, '')
        with self.assertRaises(PublicationInputError): self.publisher.publish(payload, self.control)
        self.assertEqual(self.events, [])


if __name__ == '__main__':
    unittest.main()
