"""The four Cloud-Agent transfer methods, and the status they turn on.

Most of these tests are about which failure each HTTP status *becomes*, because
that is the only thing separating a download that stops from a download that
keeps running after its lease is gone. The rest are about the frozen request
shapes -- `claim` has no body, `progress` has exactly two fields -- because
`Completion` and `Progress` are `additionalProperties: false`, so a guessed field
is a schema violation rather than a harmless extra.
"""

from __future__ import annotations

import io
import json
import unittest
from datetime import date
from typing import Mapping
from unittest import mock
from urllib import error as urlerror

from wt_media_agent.clients.cloud import (
    CloudAgentClient,
    SessionInvalidError,
    TransferIntegrityRejectedError,
    TransferLeaseLostError,
    TransferUnavailableError,
)
from wt_media_agent.clients.cloud.transfer import completion_body, parse_lease, parse_terminal

CREDENTIAL = "node-credential-SENTINEL"
TASK_ID = "task-1"

CLAIM_PATH = "/api/v1/cloud-agent/file-transfer-tasks/claim"


def lease(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "task_id": TASK_ID,
        "asset_type": "material",
        "asset_id": 42,
        "title": "春日",
        "total_bytes": 1024,
        "expected_sha256": "a" * 64,
        "lease_seconds": 60,
        "download_url": "http://cdn.test/f?signature=SHORTLIVED",
        "download_url_expires_at": "2026-09-27T10:00:00Z",
        "max_attempts": 3,
        "attempt_count": 0,
    }
    body.update(overrides)
    return body


class RecordingTransferTransport:
    """Answers with queued `(status, body)` pairs, recording how it was asked."""

    def __init__(self, *responses: tuple[int, Mapping[str, object]]) -> None:
        self.calls: list[tuple[str, str, Mapping[str, object], Mapping[str, str]]] = []
        self._responses = list(responses) or [(200, {"errcode": 0, "data": {}})]

    def __call__(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object],
        headers: Mapping[str, str],
    ) -> tuple[int, Mapping[str, object]]:
        self.calls.append((method, path, payload, headers))
        if not self._responses:
            raise AssertionError(f"the client made an unexpected extra call to {path}")
        return self._responses.pop(0)


def silent_primary_transport(*_args: object, **_kwargs: object) -> Mapping[str, object]:
    """The non-transfer transport, made unreachable on purpose.

    These tests are about the transfer surface, so the primary transport is
    injected -- the rule this repo holds every test to -- and refused if it is
    ever reached, which would mean a call went down the wrong path.
    """
    raise AssertionError("this test must not use the primary Cloud transport")


def client_with(*responses: tuple[int, Mapping[str, object]]) -> tuple[CloudAgentClient, RecordingTransferTransport]:
    transport = RecordingTransferTransport(*responses)
    return (
        CloudAgentClient(
            "http://cloud.test",
            transport=silent_primary_transport,
            transfer_transport=transport,
        ),
        transport,
    )


class ClaimTest(unittest.TestCase):
    def test_the_credential_is_the_only_identity_the_call_carries(self) -> None:
        """`claim` takes no path parameter and no body, so this is all Cloud has.

        Sending a body here would be a request the contract does not define, and
        an `agent_id` in one would be an identity this surface never asked for.
        """
        client, transport = client_with((200, {"data": {"task": None}}))

        self.assertIsNone(client.claim_transfer_task(CREDENTIAL))

        method, path, payload, headers = transport.calls[0]
        self.assertEqual((method, path), ("POST", CLAIM_PATH))
        self.assertEqual(payload, {})
        self.assertEqual(headers, {"authorization": f"Bearer {CREDENTIAL}"})

    def test_an_empty_queue_is_an_answer_and_not_a_failure(self) -> None:
        """`task: null` is how Cloud says "nothing waiting"; it must not raise."""
        client, _ = client_with((200, {"data": {"task": None}}))

        self.assertIsNone(client.claim_transfer_task(CREDENTIAL))

    def test_a_lease_is_parsed_into_the_facts_the_download_needs(self) -> None:
        client, _ = client_with((200, {"data": {"task": lease()}}))

        claimed = client.claim_transfer_task(CREDENTIAL)

        assert claimed is not None
        self.assertEqual(claimed.task_id, TASK_ID)
        self.assertEqual(claimed.total_bytes, 1024)
        self.assertEqual(claimed.expected_sha256, "a" * 64)
        self.assertEqual(claimed.download_url, "http://cdn.test/f?signature=SHORTLIVED")
        self.assertEqual(claimed.lease_seconds, 60)
        self.assertEqual(claimed.max_attempts, 3)

    def test_a_lease_missing_a_required_fact_is_refused_not_defaulted(self) -> None:
        """A defaulted `total_bytes` silently disarms the size check."""
        for field in ("total_bytes", "expected_sha256", "download_url", "task_id"):
            with self.subTest(field=field):
                body = lease(**{field: None})
                client, _ = client_with((200, {"data": {"task": body}}))
                with self.assertRaises(ValueError):
                    client.claim_transfer_task(CREDENTIAL)

    def test_a_transfer_for_anything_but_a_material_is_refused(self) -> None:
        client, _ = client_with((200, {"data": {"task": lease(asset_type="video")}}))

        with self.assertRaises(ValueError):
            client.claim_transfer_task(CREDENTIAL)

    def test_a_refused_credential_is_a_session_failure(self) -> None:
        client, _ = client_with((401, {"message": "credential is not valid"}))

        with self.assertRaises(SessionInvalidError):
            client.claim_transfer_task(CREDENTIAL)


class HeartbeatTest(unittest.TestCase):
    def test_a_renewed_lease_answers_with_nothing_and_that_is_success(self) -> None:
        """A `200` here has no content at all -- there is no body to decode."""
        client, transport = client_with((200, {}))

        self.assertIsNone(client.heartbeat_transfer_task(CREDENTIAL, TASK_ID, 512))

        method, path, payload, headers = transport.calls[0]
        self.assertEqual(
            (method, path),
            ("POST", f"/api/v1/cloud-agent/file-transfer-tasks/{TASK_ID}/heartbeat"),
        )
        self.assertEqual(payload, {"completed_bytes": 512})
        self.assertEqual(headers, {"authorization": f"Bearer {CREDENTIAL}"})

    def test_a_lost_lease_is_not_swallowed(self) -> None:
        """This is the fact the executor's loop turns on.

        If a `409` came back as a generic transport failure the executor could
        reasonably retry it, and go on writing bytes for a task whose lease
        belongs to somebody else.
        """
        client, _ = client_with((409, {"message": "task is cancelled"}))

        with self.assertRaises(TransferLeaseLostError):
            client.heartbeat_transfer_task(CREDENTIAL, TASK_ID, 512)


class ProgressTest(unittest.TestCase):
    def test_progress_carries_exactly_the_two_frozen_fields(self) -> None:
        """`Progress` is `additionalProperties: false`, and it has no total.

        The denominator is fixed on the lease, so an executor that sent one here
        would be sending a field the contract rejects.
        """
        client, transport = client_with((200, {}))

        client.report_transfer_progress(CREDENTIAL, TASK_ID, 512, 4096)

        method, path, payload, _ = transport.calls[0]
        self.assertEqual(
            (method, path),
            ("POST", f"/api/v1/cloud-agent/file-transfer-tasks/{TASK_ID}/progress"),
        )
        self.assertEqual(payload, {"completed_bytes": 512, "bytes_per_second": 4096})

    def test_progress_does_not_renew_the_lease_and_says_so_on_409(self) -> None:
        client, _ = client_with((409, {}))

        with self.assertRaises(TransferLeaseLostError):
            client.report_transfer_progress(CREDENTIAL, TASK_ID, 512, 4096)


class CompleteTest(unittest.TestCase):
    def terminal(
        self, http_status: int = 200, **terminal_overrides: object
    ) -> tuple[CloudAgentClient, RecordingTransferTransport]:
        body: dict[str, object] = {
            "task_id": TASK_ID,
            "status": "success",
            "completed_bytes": 1024,
            "file_name": "春日-42.mp4",
        }
        body.update(terminal_overrides)
        return client_with((http_status, {"errcode": 0, "data": body}))

    def test_a_successful_transfer_reports_its_size_digest_and_file_name(self) -> None:
        client, transport = self.terminal()

        terminal = client.complete_transfer_task(
            CREDENTIAL,
            TASK_ID,
            status="success",
            completed_bytes=1024,
            sha256="b" * 64,
            file_name="春日-42.mp4",
        )

        _, path, payload, _ = transport.calls[0]
        self.assertEqual(
            path, f"/api/v1/cloud-agent/file-transfer-tasks/{TASK_ID}/complete"
        )
        self.assertEqual(
            payload,
            {
                "status": "success",
                "completed_bytes": 1024,
                "sha256": "b" * 64,
                "file_name": "春日-42.mp4",
            },
        )
        self.assertEqual(terminal.task_id, TASK_ID)
        self.assertEqual(terminal.status, "success")
        self.assertEqual(terminal.completed_bytes, 1024)
        self.assertEqual(terminal.file_name, "春日-42.mp4")

    def test_unset_fields_are_absent_rather_than_null(self) -> None:
        """`additionalProperties: false` makes a null field a schema violation."""
        client, transport = self.terminal(200, status="failed")

        client.complete_transfer_task(
            CREDENTIAL, TASK_ID, status="failed", error_code="download_stalled"
        )

        payload = transport.calls[0][2]
        self.assertEqual(payload, {"status": "failed", "error_code": "download_stalled"})
        self.assertNotIn("sha256", payload)
        self.assertNotIn("completed_bytes", payload)

    def test_rejected_bytes_are_their_own_outcome(self) -> None:
        """`422` says this machine produced something wrong, which is not a lease
        problem and not something a retry of the same bytes would fix."""
        client, _ = client_with((422, {"message": "digest mismatch"}))

        with self.assertRaises(TransferIntegrityRejectedError):
            client.complete_transfer_task(
                CREDENTIAL, TASK_ID, status="success", completed_bytes=1, sha256="c" * 64
            )

    def test_a_lost_lease_and_a_server_fault_are_told_apart(self) -> None:
        """One ends the attempt, the other is worth retrying."""
        client, _ = client_with((409, {}))
        with self.assertRaises(TransferLeaseLostError):
            client.complete_transfer_task(
                CREDENTIAL, TASK_ID, status="success", completed_bytes=1, sha256="c" * 64
            )

        client, _ = client_with((503, {"message": "try later"}))
        with self.assertRaises(TransferUnavailableError):
            client.complete_transfer_task(
                CREDENTIAL, TASK_ID, status="success", completed_bytes=1, sha256="c" * 64
            )


class RefusalTest(unittest.TestCase):
    def test_no_refusal_quotes_the_credential(self) -> None:
        """The credential is the identity here, so a message is where it would go."""
        for status in (401, 409, 422, 500):
            with self.subTest(status=status):
                client, _ = client_with((status, {"message": "refused"}))
                with self.assertRaises(Exception) as caught:
                    client.heartbeat_transfer_task(CREDENTIAL, TASK_ID, 1)
                self.assertNotIn(CREDENTIAL, str(caught.exception))

    def test_clouds_own_description_survives_into_the_message(self) -> None:
        """It is the operator's only account of the refusal."""
        client, _ = client_with((409, {"message": "task is cancelled"}))

        with self.assertRaises(TransferLeaseLostError) as caught:
            client.heartbeat_transfer_task(CREDENTIAL, TASK_ID, 1)

        self.assertIn("task is cancelled", str(caught.exception))
        self.assertIn("409", str(caught.exception))


class CompletionBodyTest(unittest.TestCase):
    def test_a_success_without_a_digest_is_refused_here(self) -> None:
        """Checked where the mistake is still attributable.

        Cloud would answer `422` for the same body, but a `422` cannot say which
        field it disliked, and this one is the executor's own bug.
        """
        with self.assertRaises(ValueError):
            completion_body(status="success", completed_bytes=1)

    def test_a_failure_without_an_error_code_is_refused_here(self) -> None:
        with self.assertRaises(ValueError):
            completion_body(status="failed")

    def test_an_unknown_status_is_refused_here(self) -> None:
        """A status outside the enum, carrying everything the enum *does* demand.

        The fields are supplied deliberately: without them the failure and
        success checks would refuse this body first, and the test would pass
        while the status check it names was never reached.
        """
        with self.assertRaises(ValueError):
            completion_body(status="done", error_code="download_stalled")


class LeaseParsingTest(unittest.TestCase):
    def test_the_optional_attempt_facts_have_defined_defaults(self) -> None:
        """`max_attempts` absent must not mean "no attempts allowed"."""
        parsed = parse_lease({k: v for k, v in lease().items() if k != "max_attempts"})

        self.assertEqual(parsed.max_attempts, 1)

    def test_a_zero_byte_lease_is_a_number_and_not_a_missing_fact(self) -> None:
        """`minimum: 0` makes an empty download legal.

        A required-field check that asked whether the value were truthy would
        refuse it, and refuse it as "missing", which is the one message that
        would not lead anyone to the real cause.
        """
        self.assertEqual(parse_lease(lease(total_bytes=0)).total_bytes, 0)

    def test_the_game_name_is_optional_and_defaults_to_empty(self) -> None:
        """A material without a game sends no `game_name`; the executor falls back."""
        self.assertEqual(parse_lease(lease()).game_name, "")
        self.assertEqual(parse_lease(lease(game_name="三角洲行动")).game_name, "三角洲行动")

    def test_the_published_at_is_optional_and_becomes_a_calendar_date(self) -> None:
        """Naming is cosmetic, so the parse degrades instead of refusing.

        An absent `published_at` is `None` (the file name omits the segment), an
        unparseable one is also `None` (the same), and a parseable one loses its
        clock time -- the name only carries the day.
        """
        self.assertIsNone(parse_lease(lease()).published_at)
        parsed = parse_lease(lease(published_at="2026-09-22T08:00:00Z"))
        self.assertEqual(parsed.published_at, date(2026, 9, 22))
        self.assertIsNone(parse_lease(lease(published_at="not-a-date")).published_at)


class TerminalParsingTest(unittest.TestCase):
    def test_a_terminal_missing_a_recorded_fact_is_refused(self) -> None:
        """Cloud echoed something this executor cannot confirm against."""
        complete = {"task_id": TASK_ID, "status": "success", "completed_bytes": 1}
        for field in ("task_id", "status", "completed_bytes"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    parse_terminal({**complete, field: None})

    def test_a_zero_byte_terminal_is_a_number_and_not_a_missing_fact(self) -> None:
        parsed = parse_terminal({"task_id": TASK_ID, "status": "success", "completed_bytes": 0})

        self.assertEqual(parsed.completed_bytes, 0)

    def test_the_file_name_is_optional_but_its_absence_is_not_a_name(self) -> None:
        """A transfer that failed wrote no file, so it reported no name."""
        parsed = parse_terminal({"task_id": TASK_ID, "status": "failed", "completed_bytes": 0})

        self.assertIsNone(parsed.file_name)


class HttpTransferTransportTest(unittest.TestCase):
    """The default transport, which is the one that decides an empty body is fine."""

    class Response:
        def __init__(self, status: int, body: bytes) -> None:
            self.status = status
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self) -> "HttpTransferTransportTest.Response":
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

    def test_an_empty_two_hundred_is_a_success_not_a_parse_failure(self) -> None:
        """`heartbeat` and `progress` answer exactly like this.

        `json.loads("")` raises, so a transport that always decoded would turn
        every successful lease renewal into an error.
        """
        client = CloudAgentClient("http://cloud.test", transport=silent_primary_transport)
        with mock.patch(
            "wt_media_agent.clients.cloud.client.request.urlopen",
            return_value=self.Response(200, b""),
        ):
            status, body = client._http_transfer_transport("POST", "/x", {}, {})

        self.assertEqual(status, 200)
        self.assertEqual(body, {})

    def test_a_refusal_keeps_its_status(self) -> None:
        """The whole point of this transport: `409` must not arrive as `200`."""
        client = CloudAgentClient("http://cloud.test", transport=silent_primary_transport)
        refusal = urlerror.HTTPError(
            "http://cloud.test/x",
            409,
            "Conflict",
            {},
            io.BytesIO(json.dumps({"message": "task is cancelled"}).encode("utf-8")),
        )
        with mock.patch(
            "wt_media_agent.clients.cloud.client.request.urlopen", side_effect=refusal
        ):
            status, body = client._http_transfer_transport("POST", "/x", {}, {})

        self.assertEqual(status, 409)
        self.assertEqual(body, {"message": "task is cancelled"})


if __name__ == "__main__":
    unittest.main()
