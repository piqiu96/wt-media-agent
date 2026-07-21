from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wt_media_agent.local_api.server import LocalApiServer


class ProxyCheckTests(unittest.TestCase):
    @patch("wt_media_agent.local_api.server.check_proxy_connectivity", return_value="reachable")
    def test_sync_check_returns_connectivity_without_credentials(self, check):
        status, payload = LocalApiServer().proxy_check_response({
            "proxy_id": "proxy-1", "host": "127.0.0.1", "port": 8080,
            "username": "secret-user", "password": "secret-password",
        })

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"data": {"proxy_id": "proxy-1", "connectivity": "reachable"}})
        check.assert_called_once_with("127.0.0.1", 8080)
        self.assertNotIn("password", str(payload).lower())

    def test_sync_check_rejects_invalid_port(self):
        status, payload = LocalApiServer().proxy_check_response({"host": "127.0.0.1", "port": 0})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "proxy_input_invalid")


if __name__ == "__main__":
    unittest.main()
