"""Closing a Profile is measured, never inferred from an HTTP acknowledgement."""
import socket
import unittest
from wt_media_agent.clients.bitbrowser import BitBrowserClient


class PublicationCloseTests(unittest.TestCase):
    def test_successful_close_response_does_not_hide_a_live_debug_port(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(20)
        self.addCleanup(listener.close)
        endpoint = '127.0.0.1:' + str(listener.getsockname()[1])
        calls = []
        def transport(url, body, timeout):
            calls.append(url.rsplit('/', 1)[-1])
            return {'success': True, 'data': {}}
        client = BitBrowserClient('http://bit.test', transport=transport)
        self.assertFalse(client.close_profile_verified('owned-profile', endpoint, timeout=0.1))
        self.assertIn('close', calls)
        self.assertIn('ports', calls)

    def test_closed_original_port_and_no_profile_port_are_both_required(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        endpoint = '127.0.0.1:' + str(listener.getsockname()[1])
        listener.close()
        def transport(url, body, timeout):
            return {'success': True, 'data': {}}
        client = BitBrowserClient('http://bit.test', transport=transport)
        self.assertTrue(client.close_profile_verified('owned-profile', endpoint, timeout=0.1))

    def test_bitbrowser_failure_is_unknown_even_when_old_port_is_gone(self):
        def transport(url, body, timeout):
            return {'success': False, 'msg': 'cannot read browser state'}
        client = BitBrowserClient('http://bit.test', transport=transport)
        self.assertFalse(client.close_profile_verified('owned-profile', '127.0.0.1:1', timeout=0.1))
