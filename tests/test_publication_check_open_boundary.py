"""The durable open barrier belongs after the read-only BitBrowser preflight."""
import unittest
from unittest.mock import Mock
import test_publication_check_job as fixtures
from wt_media_agent.clients.bitbrowser.client import BitBrowserClient

class CheckOpenBoundaryTests(unittest.TestCase):
    setUp=fixtures.PublicCheckJobTests.setUp
    tearDown=fixtures.PublicCheckJobTests.tearDown
    state=fixtures.PublicCheckJobTests.state
    run_check=fixtures.PublicCheckJobTests.run_check
    def client(self):
        client=BitBrowserClient('http://127.0.0.1:54345',transport=Mock(side_effect=AssertionError('unexpected network transport')))
        client._post=Mock(return_value={'success':True,'data':{}})
        client._post_with_timeout=Mock(return_value={'success':True,'data':{'http':'127.0.0.1:9999'}})
        self.bit.open_profile_for_check=client.open_profile_for_check
        return client
    def test_preflight_disconnect_keeps_no_open_proof_and_can_retry_same_key(self):
        client=self.client();client._post.side_effect=TimeoutError('ports unavailable')
        self.run_check()
        self.assertEqual(self.state()['phase'],'active')
        client._post_with_timeout.assert_not_called()
        key=self.state()['request_key']
        client._post.side_effect=None
        self.run_check()
        self.assertEqual(self.state()['phase'],'settled')
        self.assertEqual(self.state()['request_key'],key)
        client._post_with_timeout.assert_called_once()
    def test_real_open_reply_loss_keeps_possible_open_and_never_reopens(self):
        client=self.client();client._post_with_timeout.side_effect=TimeoutError('open reply lost')
        self.run_check();self.run_check()
        self.assertEqual(self.state()['phase'],'possible_open')
        client._post_with_timeout.assert_called_once()
        self.bit.close_profile_verified.assert_not_called()
    def test_existing_manual_window_never_crosses_possible_open_barrier(self):
        client=self.client();client._post.return_value={'success':True,'data':{'original-profile':9998}}
        self.run_check()
        self.assertEqual(self.state()['phase'],'active')
        client._post_with_timeout.assert_not_called()
        self.bit.close_profile_verified.assert_not_called()
