from __future__ import annotations

import json
import unittest
from typing import Mapping
from unittest import mock
from urllib import request as urlrequest

from wt_media_agent.clients.cloud import (
    AgentAuthError,
    CloudAgentClient,
    CloudUnavailableError,
)

TOKEN = "v1.payload.signature"


class RecordingTransport:
    def __init__(self, answer: Mapping[str, object]) -> None:
        self.calls: list[tuple[str, str, Mapping[str, object], Mapping[str, str]]] = []
        self.answer = answer

    def __call__(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object],
        headers: Mapping[str, str],
    ) -> Mapping[str, object]:
        self.calls.append((method, path, payload, headers))
        return self.answer


class BindingTest(unittest.TestCase):
    def test_bit_account_bind_carries_the_observed_main_user(self) -> None:
        transport = RecordingTransport({"data": {"bound": True}})

        CloudAgentClient("http://cloud.test", transport=transport).bind_bit_account(
            TOKEN,
            main_user_id="main-1",
            observed_at="2026-10-23T00:00:00Z",
            rebind=True,
        )

        self.assertEqual(transport.calls[0][1], "/api/v1/local-agent/bit-account/bind")
        self.assertEqual(
            transport.calls[0][2],
            {
                "bit_main_user_id": "main-1",
                "observed_at": "2026-10-23T00:00:00Z",
                "rebind": True,
            },
        )


class FailureTest(unittest.TestCase):
    def test_missing_data_is_a_transport_failure(self) -> None:
        client = CloudAgentClient(
            "http://cloud.test", transport=lambda method, path, payload, headers: {}
        )

        with self.assertRaises(CloudUnavailableError):
            client.bind_bit_account(
                TOKEN, main_user_id="main-1", observed_at="2026-10-23T00:00:00Z"
            )

    def test_an_auth_error_names_its_code(self) -> None:
        client = CloudAgentClient(
            "http://cloud.test",
            transport=lambda method, path, body, headers: {
                "errcode": 11001,
                "message": "你的账号已在另一台电脑登录",
                "error": {"code": "AUTH_DESKTOP_REPLACED"},
            },
        )

        with self.assertRaises(AgentAuthError) as raised:
            client.bind_bit_account(
                TOKEN, main_user_id="main-1", observed_at="2026-10-23T00:00:00Z"
            )

        self.assertEqual(raised.exception.code, "AUTH_DESKTOP_REPLACED")


class RequestTimeoutTest(unittest.TestCase):
    def _call(self, client: CloudAgentClient) -> float:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"data": {}}).encode()
        with mock.patch.object(urlrequest, "urlopen", return_value=response) as urlopen:
            client.bind_bit_account(
                TOKEN, main_user_id="main-1", observed_at="2026-10-23T00:00:00Z"
            )
        return urlopen.call_args.kwargs["timeout"]

    def test_the_configured_timeout_reaches_the_request(self) -> None:
        # network-ok: the real transport is the subject; `_call` patches urlopen
        self.assertEqual(self._call(CloudAgentClient("http://cloud.test", timeout=42.5)), 42.5)

    def test_an_omitted_timeout_keeps_the_historical_default(self) -> None:
        # network-ok: the real transport is the subject; `_call` patches urlopen
        self.assertEqual(self._call(CloudAgentClient("http://cloud.test")), 10)

class OriginalPublicationReadTests(unittest.TestCase):
    def test_original_attempt_read_uses_bearer_exact_ids_and_no_request_body(self):
        reply=mock.MagicMock()
        reply.__enter__.return_value.read.return_value=b'{"errcode":0,"data":{"attempt_id":"9007199254740993","scene_resolved":false}}'
        with mock.patch.object(urlrequest,'urlopen',return_value=reply) as open_url:
            # Explicit HTTP adapter under the patched opener: inspect the actual
            # request without adding an unmocked/default-transport exception.
            client = CloudAgentClient('http://cloud.test', transport=lambda *args: client._http_transport(*args))
            result=client.original_publication_attempt(TOKEN,'9','9007199254740993')
        req=open_url.call_args.args[0]
        self.assertEqual(req.get_method(),'GET')
        self.assertTrue(req.full_url.endswith('/publication-tasks/9/attempts/9007199254740993'))
        self.assertEqual(req.get_header('Authorization'),'Bearer '+TOKEN)
        self.assertIsNone(req.data)
        self.assertFalse(result['scene_resolved'])


if __name__ == "__main__":
    unittest.main()
