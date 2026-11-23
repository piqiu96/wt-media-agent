"""Local API startup must not wait for reverse DNS on a loopback address."""
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from wt_media_agent.local_api.server import serve
from wt_media_agent.local_api.state import LocalAgentState


class LocalServerStartupTest(unittest.TestCase):
    def test_serving_loopback_never_requires_reverse_dns(self):
        entered = []

        def stop_after_binding(server):
            entered.append(server.server_address)
            self.assertEqual(server.server_name, '127.0.0.1')
            self.assertEqual(server.server_port, server.server_address[1])

        with patch('socket.getfqdn', side_effect=AssertionError('reverse DNS must not block local startup')):
            with patch.object(ThreadingHTTPServer, 'serve_forever', stop_after_binding):
                serve('127.0.0.1', 0, bitbrowser=object(), state=LocalAgentState(agent_id='startup-test', status='idle'))
        self.assertEqual(len(entered), 1)
        self.assertGreater(entered[0][1], 0)
