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
        self.assertTrue(req.full_url.endswith('/publication-task/9/attempt/9007199254740993'))
        self.assertEqual(req.get_header('Authorization'),'Bearer '+TOKEN)
        self.assertIsNone(req.data)
        self.assertFalse(result['scene_resolved'])


class PublicationReportRouteTests(unittest.TestCase):
    def test_claim_report_and_completion_use_singular_resource(self):
        transport = RecordingTransport({'errcode': 0, 'data': {}})
        client = CloudAgentClient('http://cloud.test', transport=transport)
        client.claim_publication(TOKEN, 'stable-claim')
        alive = {'attempt_id': '9007199254740993', 'report_seq': 1}
        ready = dict(alive, report_seq=2, stage='ready', platform_uid='real-uid',
                     bit_profile_id='profile-1', video_sha256='a'*64, cover_sha256='b'*64)
        completion = dict(alive, report_seq=3, result_hash='c'*64,
                          result={'status': 'uncertain', 'evidence': {'scene_closed': False}})
        client.report_publication(TOKEN, '9', 'report', alive)
        client.report_publication(TOKEN, '9', 'report', ready)
        client.report_publication(TOKEN, '9', 'complete', completion)
        self.assertEqual([call[1] for call in transport.calls], [
            '/api/v1/cloud-agent/publication-task/claim',
            '/api/v1/cloud-agent/publication-task/9/report',
            '/api/v1/cloud-agent/publication-task/9/report',
            '/api/v1/cloud-agent/publication-task/9/complete',
        ])
        self.assertEqual([call[2] for call in transport.calls],
                         [{'request_key': 'stable-claim'}, alive, ready, completion])
        self.assertTrue(all(call[0] == 'POST' and call[3]['authorization'] == 'Bearer '+TOKEN
                            for call in transport.calls))

    def test_retired_report_operations_are_rejected_before_transport(self):
        transport = RecordingTransport({'data': {}})
        client = CloudAgentClient('http://cloud.test', transport=transport)
        for operation in ('heartbeat', 'progress'):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                client.report_publication(TOKEN, '9', operation, {'attempt_id': '1', 'report_seq': 1})
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
