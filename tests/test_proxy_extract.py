from __future__ import annotations

from pathlib import Path
import sys
import json
import threading
import unittest
from unittest.mock import patch
from urllib import error as urlerror
from urllib import request as urlrequest
from http.server import ThreadingHTTPServer


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from support import UnusedBitBrowser

from wt_media_agent.local_api.state import LocalAgentState
from wt_media_agent.local_api.server import LocalApiServer, make_handler


class ProxyExtractTests(unittest.TestCase):
    def test_local_api_exposes_dynamic_proxy_extraction(self) -> None:
        api = LocalApiServer(LocalAgentState(), bitbrowser=UnusedBitBrowser())

        self.assertTrue(callable(getattr(api, "proxy_extract_response", None)))

    @patch("wt_media_agent.local_api.server.urlrequest.urlopen")
    def test_extracts_first_plain_text_address_with_requested_protocol(self, urlopen) -> None:
        response = urlopen.return_value.__enter__.return_value
        response.read.return_value = b"\n  203.0.113.9:1080:user:pass\n198.51.100.8:80\n"

        status, payload = LocalApiServer(LocalAgentState(), bitbrowser=UnusedBitBrowser()).proxy_extract_response({
            "extract_url": "https://provider.example/extract?token=secret",
            "proxy_protocol": "socks5",
        })

        self.assertEqual(status, 200)
        self.assertEqual(payload["data"], {
            "proxy_protocol": "socks5",
            "host": "203.0.113.9",
            "port": 1080,
            "username": "user",
            "password": "pass",
        })
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://provider.example/extract?token=secret")

    @patch("wt_media_agent.local_api.server.urlrequest.urlopen")
    def test_rejects_non_http_source_without_fetching(self, urlopen) -> None:
        status, payload = LocalApiServer(LocalAgentState(), bitbrowser=UnusedBitBrowser()).proxy_extract_response({"extract_url": "file:///tmp/proxy.txt"})

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "proxy_extract_input_invalid")
        urlopen.assert_not_called()

    def test_loopback_endpoint_routes_dynamic_extraction(self) -> None:
        api = LocalApiServer(LocalAgentState(), bitbrowser=UnusedBitBrowser())
        with patch.object(api, "proxy_extract_response", return_value=(200, {"data": {"host": "203.0.113.9", "port": 1080}})) as extract:
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                request = urlrequest.Request(
                    f"http://127.0.0.1:{server.server_port}/api/v1/proxy-extract",
                    data=json.dumps({"extract_url": "https://provider.example/extract"}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urlrequest.urlopen(request) as response:
                        status, body = response.status, response.read()
                except urlerror.HTTPError as error:
                    status, body = error.code, error.read()
            finally:
                server.shutdown()
                thread.join()
                server.server_close()

        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["data"]["host"], "203.0.113.9")
        extract.assert_called_once_with({"extract_url": "https://provider.example/extract"})


if __name__ == "__main__":
    unittest.main()
