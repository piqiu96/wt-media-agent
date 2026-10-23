from __future__ import annotations

import json
import unittest
from typing import Mapping
from unittest import mock
from urllib import request as urlrequest

from wt_media_agent.clients.cloud import (
    AgentAuthError,
    CloudAgentClient,
    NoPendingTaskError,
    StaleAttemptError,
)

TOKEN = "v1.payload.signature"
ATTEMPT_ID = "attempt-1"


class RecordingTransport:
    def __init__(self, answer: Mapping[str, object] | None = None) -> None:
        self.calls: list[tuple[str, str, Mapping[str, object], Mapping[str, str]]] = []
        self.answer = answer if answer is not None else {"data": {"task_id": "task-1"}}

    def __call__(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object],
        headers: Mapping[str, str],
    ) -> Mapping[str, object]:
        self.calls.append((method, path, payload, headers))
        return self.answer


class ClaimTest(unittest.TestCase):
    def test_claim_carries_only_the_lease_and_the_bearer_token(self) -> None:
        transport = RecordingTransport()

        CloudAgentClient("http://cloud.test", transport=transport).claim_task(
            TOKEN, lease_seconds=30
        )

        self.assertEqual(transport.calls[0][0], "POST")
        self.assertEqual(transport.calls[0][1], "/api/v1/cloud-agent/tasks/claim")
        self.assertEqual(transport.calls[0][2], {"lease_seconds": 30})
        self.assertEqual(transport.calls[0][3], {"authorization": f"Bearer {TOKEN}"})

    def test_an_empty_poll_is_no_task_and_not_an_error(self) -> None:
        """Cloud 用 409/30001 表示没有任务；该码没有 `error.code`，只能按 errcode 识别。"""
        transport = RecordingTransport(
            {"errcode": 30001, "message": "没有可领取的任务", "data": None}
        )

        lease = CloudAgentClient("http://cloud.test", transport=transport).claim_task(TOKEN)

        self.assertIsNone(lease)

    def test_a_response_without_a_task_is_no_task(self) -> None:
        transport = RecordingTransport({"data": {}})

        self.assertIsNone(
            CloudAgentClient("http://cloud.test", transport=transport).claim_task(TOKEN)
        )


class ReportTest(unittest.TestCase):
    def test_report_carries_attempt_and_status(self) -> None:
        transport = RecordingTransport({"data": {"task_id": "task-1", "status": "running"}})

        CloudAgentClient("http://cloud.test", transport=transport).report_task(
            TOKEN, "task-1", ATTEMPT_ID, status="running", progress=50, message="halfway"
        )

        self.assertEqual(transport.calls[0][1], "/api/v1/cloud-agent/tasks/task-1/report")
        self.assertEqual(
            transport.calls[0][2],
            {
                "attempt_id": ATTEMPT_ID,
                "status": "running",
                "progress": 50,
                "message": "halfway",
            },
        )

    def test_optional_error_fields_are_omitted_when_unset(self) -> None:
        transport = RecordingTransport({"data": {"task_id": "task-1"}})

        CloudAgentClient("http://cloud.test", transport=transport).report_task(
            TOKEN, "task-1", ATTEMPT_ID, status="running"
        )

        self.assertNotIn("error_code", transport.calls[0][2])
        self.assertNotIn("retryable", transport.calls[0][2])
        self.assertNotIn("result", transport.calls[0][2])

    def test_a_terminal_report_carries_the_error_contract_and_result(self) -> None:
        transport = RecordingTransport({"data": {"task_id": "task-1"}})

        CloudAgentClient("http://cloud.test", transport=transport).report_task(
            TOKEN,
            "task-1",
            ATTEMPT_ID,
            status="failed",
            error_code="RESULT_UNKNOWN",
            error_message="check the platform",
            retryable=False,
            recovery_action="check_platform_state",
            result={"account_id": "acc-1"},
        )

        payload = transport.calls[0][2]
        self.assertEqual(payload["error_code"], "RESULT_UNKNOWN")
        self.assertEqual(payload["error_message"], "check the platform")
        self.assertIs(payload["retryable"], False)
        self.assertEqual(payload["recovery_action"], "check_platform_state")
        self.assertEqual(payload["result"], {"account_id": "acc-1"})

    def test_a_stale_attempt_is_its_own_error(self) -> None:
        transport = RecordingTransport(
            {
                "errcode": 30009,
                "message": "attempt superseded",
                "error": {"code": "STALE_ATTEMPT"},
            }
        )

        with self.assertRaises(StaleAttemptError):
            CloudAgentClient("http://cloud.test", transport=transport).report_task(
                TOKEN, "task-1", ATTEMPT_ID, status="succeeded"
            )


class ActivityTest(unittest.TestCase):
    def test_activity_accepts_a_null_data_body(self) -> None:
        transport = RecordingTransport({"errcode": 0, "data": None})

        result = CloudAgentClient("http://cloud.test", transport=transport).report_activity(
            TOKEN, "task-1", ATTEMPT_ID, message="still running", stage="cookies"
        )

        self.assertIsNone(result)
        self.assertEqual(transport.calls[0][1], "/api/v1/cloud-agent/tasks/task-1/activity")
        self.assertEqual(
            transport.calls[0][2],
            {"attempt_id": ATTEMPT_ID, "message": "still running", "stage": "cookies"},
        )

    def test_activity_refreshes_without_a_stage(self) -> None:
        transport = RecordingTransport({"data": None})

        CloudAgentClient("http://cloud.test", transport=transport).report_activity(
            TOKEN, "task-1", ATTEMPT_ID
        )

        self.assertEqual(transport.calls[0][2], {"attempt_id": ATTEMPT_ID})


class PreflightAndBindingTest(unittest.TestCase):
    def test_execution_preflight_sends_the_action_and_the_facts(self) -> None:
        transport = RecordingTransport({"data": {"allowed": True}})

        CloudAgentClient("http://cloud.test", transport=transport).execution_preflight(
            TOKEN,
            action="cookie_read",
            profile_id="profile-1",
            facts={"bitbrowser_status": "running", "observed_at": "2026-10-23T00:00:00Z"},
        )

        self.assertEqual(transport.calls[0][1], "/api/v1/local-agent/execution-preflight")
        self.assertEqual(
            transport.calls[0][2],
            {
                "action": "cookie_read",
                "task_id": "",
                "attempt_id": "",
                "profile_id": "profile-1",
                "media_account_id": "",
                "facts": {
                    "bitbrowser_status": "running",
                    "observed_at": "2026-10-23T00:00:00Z",
                },
            },
        )

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
                "main_user_id": "main-1",
                "observed_at": "2026-10-23T00:00:00Z",
                "rebind": True,
            },
        )

    def test_sensitive_preflight_sends_facts_and_renew_finish_do_not_send_a_credential(self) -> None:
        transport = RecordingTransport({"data": {"outcome": "granted", "permit_id": "permit-1"}})
        client = CloudAgentClient("http://cloud.test", transport=transport)

        client.preflight_sensitive_task(TOKEN, "task-1", {"bitbrowser_status": "running"})
        client.renew_sensitive_permit(TOKEN, "permit-1", 60)
        client.finish_sensitive_permit(TOKEN, "permit-1", "completed")

        self.assertEqual(transport.calls[0][1], "/api/v1/local-agent/sensitive-tasks/task-1/preflight")
        self.assertEqual(transport.calls[0][2], {"facts": {"bitbrowser_status": "running"}})
        self.assertEqual(transport.calls[1][1], "/api/v1/local-agent/sensitive-permits/permit-1/renew")
        self.assertEqual(transport.calls[1][2], {"lease_seconds": 60})
        self.assertEqual(transport.calls[2][1], "/api/v1/local-agent/sensitive-permits/permit-1/finish")
        self.assertEqual(transport.calls[2][2], {"outcome": "completed"})
        for _, _, _, headers in transport.calls:
            self.assertEqual(headers, {"authorization": f"Bearer {TOKEN}"})


class FailureTest(unittest.TestCase):
    def test_missing_data_is_rejected(self) -> None:
        client = CloudAgentClient(
            "http://cloud.test", transport=lambda method, path, payload, headers: {}
        )

        with self.assertRaises(ValueError):
            client.report_task(TOKEN, "task-1", ATTEMPT_ID, status="running")

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
            client.claim_task(TOKEN)

        self.assertEqual(raised.exception.code, "AUTH_DESKTOP_REPLACED")

    def test_the_empty_poll_code_is_its_own_type(self) -> None:
        client = CloudAgentClient(
            "http://cloud.test",
            transport=lambda method, path, body, headers: {"errcode": 30001, "data": None},
        )

        with self.assertRaises(NoPendingTaskError):
            client.report_task(TOKEN, "task-1", ATTEMPT_ID, status="running")


if __name__ == "__main__":
    unittest.main()


class RequestTimeoutTest(unittest.TestCase):
    """CHG-056 T-04: the request timeout is configured, not hard-coded at 10s."""

    def _call(self, client: CloudAgentClient) -> float:
        """Make one real transport call and return the timeout it used."""
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"data": {}}).encode()
        with mock.patch.object(
            urlrequest, "urlopen", return_value=response
        ) as urlopen:
            client.claim_task(TOKEN)
        return urlopen.call_args.kwargs["timeout"]

    def test_the_configured_timeout_reaches_the_request(self) -> None:
        # network-ok: the real transport is the subject; `_call` patches urlopen
        client = CloudAgentClient("http://cloud.test", timeout=42.5)

        self.assertEqual(self._call(client), 42.5)

    def test_an_omitted_timeout_keeps_the_historical_default(self) -> None:
        """Callers that pass no timeout must keep behaving exactly as before."""
        # network-ok: the real transport is the subject; `_call` patches urlopen
        client = CloudAgentClient("http://cloud.test")

        self.assertEqual(self._call(client), 10)
