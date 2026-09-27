"""The download executor: what lands on disk, and what Cloud is told about it.

The source client is **real** in these tests and only the opener is a double, so
what is exercised is the protocol the executor actually speaks -- a ranged
request, a server that ignores the range, a 404 from an expired signature -- and
not a re-statement of it. The sink is real too, which is why several arms assert
about files rather than about calls: the property this module exists for is that
a download which did not finish is not a file, and only the filesystem can say
whether that held.

The clock and the sleeper are injected, so the throttle and the retry pause are
readings rather than wall-clock waits. Every arm that pins a *rate* or an
*interval* would otherwise be a timing test.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib import error as urlerror

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from wt_media_agent.clients.cloud import (
    TransferIntegrityRejectedError,
    TransferLease,
    TransferLeaseLostError,
    TransferUnavailableError,
)
from wt_media_agent.clients.cloud.transfer import TransferTerminal
from wt_media_agent.executors.material_download import (
    ERROR_DISK_INSUFFICIENT,
    ERROR_INTEGRITY_FAILED,
    ERROR_LEASE_UNCONFIRMED,
    ERROR_NAME_UNUSABLE,
    ERROR_SAVE_DIR_UNSET,
    ERROR_SAVE_DIR_UNWRITABLE,
    ERROR_SOURCE_UNAVAILABLE,
    ERROR_STALLED,
    EXECUTOR_ERROR_CODES,
    OUTCOME_FAILED,
    OUTCOME_LEASE_LOST,
    OUTCOME_REJECTED,
    OUTCOME_SUCCESS,
    OUTCOME_UNREPORTED,
    MaterialDownloadExecutor,
)
from wt_media_agent.storage import DownloadSink, TransferResume

TASK_ID = "task-1"
CREDENTIAL = "node-credential-SENTINEL"
#: Two chunks at the default test chunk size, so a report, a resume and a stall
#: all have somewhere to happen inside one body.
BODY = bytes(range(256)) * 16
CHUNK = 1024
SIGNED_URL = "https://cdn.test/materials/42/abc.mp4?X-Amz-Signature=SHORTLIVED"
FILE_NAME = "春日-42.mp4"

CONTRACT = ROOT / "contracts" / "local-error-codes" / "v1" / "transfer.yaml"


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def make_lease(body: bytes = BODY, **overrides: object) -> TransferLease:
    fields: dict[str, object] = {
        "task_id": TASK_ID,
        "asset_id": 42,
        "title": "春日",
        "total_bytes": len(body),
        "expected_sha256": sha256(body),
        "lease_seconds": 60,
        "download_url": SIGNED_URL,
        "download_url_expires_at": "2026-09-27T10:00:00Z",
    }
    fields.update(overrides)
    return TransferLease(**fields)  # type: ignore[arg-type]


class StepClock:
    """A monotonic clock that advances a fixed amount every time it is read.

    Injected so that "two seconds passed" is a number in the test rather than a
    sleep, and so the same reading happens on every machine.
    """

    def __init__(self, step: float = 1.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class ServingBody:
    """One response: the bytes after the offset asked for, and how they end."""

    def __init__(
        self,
        data: bytes,
        *,
        status: int,
        headers: dict[str, str],
        stall_after: int | None = None,
    ) -> None:
        self.status = status
        self.headers = headers
        self._data = data
        self._sent = 0
        self._stall_after = stall_after
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if self._stall_after is not None and self._sent >= self._stall_after:
            # What `http.client` raises when the peer goes away mid-body, and
            # what `source.py` turns into `SourceStalledError`.
            raise OSError("connection reset")
        end = self._sent + size if size >= 0 else len(self._data)
        if self._stall_after is not None:
            end = min(end, self._stall_after)
        chunk = self._data[self._sent:end]
        self._sent += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class ServingOpener:
    """A CDN holding one object, honouring `Range` unless told not to.

    `stalls` is a per-call script: the n-th call serves that many bytes and then
    drops the connection (`None` means it serves the whole body). It is a list
    rather than a number because the interesting arm is a download that stalls
    and then, on the retry, does not.
    """

    def __init__(
        self,
        body: bytes = BODY,
        *,
        bodies: "list[bytes] | None" = None,
        honour_range: bool = True,
        with_length: bool = True,
        content_range_total: int | None = None,
        stalls: "list[int | None] | None" = None,
        raises: Exception | None = None,
    ) -> None:
        self.body = body
        # A per-call script of what the object holds, for the arms where the
        # first attempt gets bytes that are wrong and the second gets the same
        # object intact. Empty means "the same object every time".
        self.bodies = list(bodies or [])
        self.honour_range = honour_range
        self.with_length = with_length
        self.content_range_total = content_range_total
        self.stalls = list(stalls or [])
        self.raises = raises
        self.calls: list[tuple[str, int, float]] = []
        self._served: list[bytes] = []

    def __call__(self, url: str, headers: dict, timeout: float) -> ServingBody:
        offset = 0
        asked = headers.get("Range") if hasattr(headers, "get") else None
        if asked:
            offset = int(str(asked).removeprefix("bytes=").rstrip("-"))
        self.calls.append((url, offset, timeout))
        if self.raises is not None:
            raise self.raises
        stall_after = self.stalls.pop(0) if self.stalls else None
        body = self.bodies.pop(0) if self.bodies else self.body
        self._served.append(body)

        # A server that ignores `Range` sends the whole object from byte zero.
        # Serving the slice anyway would make the fake agree with a caller that
        # appended it to a part file, which is the fault under test.
        served = body[offset:] if self.honour_range else body
        headers_out: dict[str, str] = {}
        status = 200
        if offset and self.honour_range:
            status = 206
            total = len(body) if self.content_range_total is None else self.content_range_total
            headers_out["Content-Range"] = f"bytes {offset}-{offset + len(served) - 1}/{total}"
        elif self.with_length:
            headers_out["Content-Length"] = str(len(served))
        return ServingBody(served, status=status, headers=headers_out, stall_after=stall_after)

    @property
    def offsets(self) -> list[int]:
        """The byte each request asked to start at, in order."""
        return [offset for _, offset, _ in self.calls]


class FakeCloud:
    """The three transfer calls a download makes, and what Cloud answers.

    `raise_on` is a one-shot: the next call of that name raises and the entry is
    consumed, which is how "the second progress report fails" is written down
    without a state machine.
    """

    def __init__(self) -> None:
        self.heartbeats: list[tuple[str, str, int]] = []
        self.progress: list[tuple[str, str, int, int]] = []
        self.completions: list[dict] = []
        self.raise_on: dict[str, Exception] = {}

    def _check(self, method: str) -> None:
        failure = self.raise_on.pop(method, None)
        if failure is not None:
            raise failure

    def heartbeat_transfer_task(self, credential: str, task_id: str, completed: int) -> None:
        self._check("heartbeat")
        self.heartbeats.append((credential, task_id, completed))

    def report_transfer_progress(
        self, credential: str, task_id: str, completed: int, rate: int
    ) -> None:
        self._check("progress")
        self.progress.append((credential, task_id, completed, rate))

    def complete_transfer_task(self, credential: str, task_id: str, **fields: object):
        self.completions.append({"credential": credential, "task_id": task_id, **fields})
        self._check("complete")
        return TransferTerminal(
            task_id=task_id,
            status=str(fields.get("status")),
            completed_bytes=int(fields.get("completed_bytes") or 0),
            file_name=fields.get("file_name"),  # type: ignore[arg-type]
        )


class DownloadTest(unittest.TestCase):
    """A save directory, a fake Cloud, and one executor wired to both."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)
        self.cloud = FakeCloud()
        self.recorded: list[TransferResume] = []
        self.directory_choice: str | None = str(self.directory)
        self.sleeps: list[float] = []

    def executor(self, opener: ServingOpener, **overrides: object) -> MaterialDownloadExecutor:
        settings: dict[str, object] = {
            "chunk_bytes": CHUNK,
            "clock": StepClock(),
            "sleeper": self.sleeps.append,
        }
        settings.update(overrides)
        return MaterialDownloadExecutor(
            self.cloud,  # type: ignore[arg-type]
            "agent-1",
            credential=CREDENTIAL,
            save_directory=lambda: self.directory_choice,
            record=self.recorded.append,
            opener=opener,
            **settings,  # type: ignore[arg-type]
        )

    def run_download(
        self,
        opener: ServingOpener,
        lease: TransferLease | None = None,
        resume: TransferResume | None = None,
        **overrides: object,
    ) -> dict:
        task: dict[str, object] = {"lease": lease or make_lease()}
        if resume is not None:
            task["resume"] = resume
        return self.executor(opener, **overrides).execute(task)  # type: ignore[return-value]

    def plant_part(self, data: bytes, task_id: str = TASK_ID) -> None:
        DownloadSink(self.directory).append(task_id, data)

    def part_bytes(self, task_id: str = TASK_ID) -> bytes:
        return DownloadSink(self.directory).part_path(task_id).read_bytes()

    def saved(self, name: str = FILE_NAME) -> bytes:
        return (self.directory / name).read_bytes()

    def completion(self) -> dict:
        self.assertEqual(len(self.cloud.completions), 1, self.cloud.completions)
        return self.cloud.completions[0]


class NamingTest(DownloadTest):
    def test_the_name_is_the_title_the_id_and_the_address_s_suffix(self) -> None:
        self.assertEqual(self.run_download(ServingOpener())["status"], OUTCOME_SUCCESS)

        self.assertEqual(self.saved(), BODY)
        self.assertEqual(self.completion()["file_name"], FILE_NAME)

    def test_a_second_download_does_not_overwrite_the_first(self) -> None:
        (self.directory / FILE_NAME).write_bytes(b"an earlier download")

        self.run_download(ServingOpener())

        self.assertEqual((self.directory / "春日-42 (2).mp4").read_bytes(), BODY)
        self.assertEqual(self.saved(), b"an earlier download")
        self.assertEqual(self.completion()["file_name"], "春日-42 (2).mp4")

    def test_a_resumed_attempt_keeps_the_name_the_first_attempt_chose(self) -> None:
        """Otherwise one download becomes two files, which the operator sees.

        The name is the part of the state that cannot be re-derived: `allocate`
        answers with the first name free *now*, so a second attempt that asked
        again would take ` (2)` if anything had claimed the first name meanwhile.
        """
        (self.directory / FILE_NAME).write_bytes(b"another material")
        self.plant_part(BODY[:CHUNK])
        resume = TransferResume("春日-42 (2).mp4", CHUNK)

        self.run_download(ServingOpener(), resume=resume)

        self.assertEqual((self.directory / "春日-42 (2).mp4").read_bytes(), BODY)

    def test_a_title_with_no_usable_name_is_refused(self) -> None:
        outcome = self.run_download(ServingOpener(), lease=make_lease(title="..."))

        self.assertEqual(outcome["status"], OUTCOME_FAILED)
        self.assertEqual(outcome["error_code"], ERROR_NAME_UNUSABLE)
        self.assertEqual(self.completion()["status"], "failed")
        self.assertEqual(self.cloud.progress, [])


class SaveDirectoryTest(DownloadTest):
    def test_an_unset_save_directory_is_refused_rather_than_guessed(self) -> None:
        """`null` is not "somewhere sensible": the executor does not pick a path.

        A file the operator cannot find is not the file they asked for, and
        quietly keeping a few gigabytes of video in a temporary directory is
        worse than saying that nothing has been chosen.
        """
        opener = ServingOpener()
        self.directory_choice = None

        outcome = self.run_download(opener)

        self.assertEqual(outcome["status"], OUTCOME_FAILED)
        self.assertEqual(outcome["error_code"], ERROR_SAVE_DIR_UNSET)
        self.assertEqual(opener.calls, [], "nothing may be fetched with nowhere to put it")
        self.assertEqual(self.completion()["error_message"], "no save directory has been chosen on this machine")

    def test_a_directory_that_is_no_longer_writable_is_refused(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores the mode bits this arm turns off")
        os.chmod(self.directory, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, self.directory, stat.S_IRWXU)
        opener = ServingOpener()

        outcome = self.run_download(opener)

        self.assertEqual(outcome["error_code"], ERROR_SAVE_DIR_UNWRITABLE)
        self.assertEqual(opener.calls, [])
        self.assertFalse(DownloadSink(self.directory).part_path(TASK_ID).exists())

    def test_no_room_is_refused_before_a_byte_is_fetched(self) -> None:
        opener = ServingOpener()
        lease = make_lease(total_bytes=1 << 62)

        outcome = self.run_download(opener, lease=lease)

        self.assertEqual(outcome["error_code"], ERROR_DISK_INSUFFICIENT)
        self.assertEqual(opener.calls, [], "the check must precede the download")

    def test_no_failure_message_carries_the_directory_or_the_address(self) -> None:
        """`error_message` travels to a Cloud table, and neither may go with it.

        The address is a short-lived credential (`secret_policy`) and the
        directory is a local absolute path with somebody's account name in it
        (CHG-061 §8). Both are checked by value, so a message that interpolated
        either would have to fail this arm.
        """
        cases: list[tuple[dict, str]] = [
            ({"save_directory": None}, ERROR_SAVE_DIR_UNSET),
            ({"lease": make_lease(title="...")}, ERROR_NAME_UNUSABLE),
            ({"lease": make_lease(total_bytes=1 << 62)}, ERROR_DISK_INSUFFICIENT),
            (
                {"opener_raises": urlerror.HTTPError(SIGNED_URL, 403, "Forbidden", {}, None)},
                ERROR_SOURCE_UNAVAILABLE,
            ),
        ]
        for overrides, expected in cases:
            with self.subTest(code=expected):
                self.cloud = FakeCloud()
                self.directory_choice = overrides.pop("save_directory", str(self.directory))
                opener = ServingOpener(raises=overrides.pop("opener_raises", None))
                lease = overrides.pop("lease", make_lease())

                outcome = self.run_download(opener, lease=lease)

                self.assertEqual(outcome["error_code"], expected)
                message = self.completion()["error_message"]  # type: ignore[assignment]
                self.assertNotIn(str(self.directory), message)
                self.assertNotIn("SHORTLIVED", message)
                self.assertNotIn("cdn.test", message)

    def test_a_task_without_a_lease_is_a_caller_fault(self) -> None:
        """The one ending that is a bug rather than an outcome: it raises.

        Everything else a download can meet is the world's, and the runner has to
        keep running; a mapping with no lease in it is this process's mistake and
        must not be reported to Cloud as a download that failed.
        """
        with self.assertRaises(ValueError):
            self.executor(ServingOpener()).execute({"task_id": TASK_ID})
        self.assertEqual(self.cloud.completions, [])


class TransferTest(DownloadTest):
    def test_the_bytes_land_exactly_as_the_source_served_them(self) -> None:
        self.run_download(ServingOpener())

        self.assertEqual(sha256(self.saved()), sha256(BODY))

    def test_the_completion_carries_the_size_the_digest_and_the_name(self) -> None:
        self.run_download(ServingOpener())

        completion = self.completion()
        self.assertEqual(completion["credential"], CREDENTIAL)
        self.assertEqual(completion["status"], "success")
        self.assertEqual(completion["completed_bytes"], len(BODY))
        self.assertEqual(completion["sha256"], sha256(BODY))

    def test_the_part_file_is_not_the_file_and_the_file_is_there_at_the_end(self) -> None:
        self.run_download(ServingOpener())

        self.assertFalse(DownloadSink(self.directory).part_path(TASK_ID).exists())
        self.assertFalse((self.directory / ".wt-media-part").exists())
        self.assertTrue((self.directory / FILE_NAME).exists())

    def test_a_body_with_the_wrong_digest_never_becomes_the_file(self) -> None:
        """The one arm the whole module exists for: a bad file must not exist.

        The declared digest is changed instead of the body, so the size check
        passes and only the digest can catch it -- which is the case a
        size-only implementation would ship.
        """
        lease = make_lease(expected_sha256="0" * 64)

        outcome = self.run_download(ServingOpener(), lease=lease)

        self.assertEqual(outcome["error_code"], ERROR_INTEGRITY_FAILED)
        self.assertFalse((self.directory / FILE_NAME).exists())
        self.assertTrue(DownloadSink(self.directory).part_path(TASK_ID).exists())

    def test_a_body_with_the_wrong_digest_is_retried_from_zero(self) -> None:
        """Bad bytes are worth another go, and the part they are in is not.

        Resuming onto a part that failed the digest would keep the bytes that
        were wrong; `discard` is what makes the second attempt start over. The
        arm is the second request's offset: zero, not the length of the part the
        first attempt left.
        """
        corrupt = BODY[:-1] + bytes([BODY[-1] ^ 0xFF])
        opener = ServingOpener(bodies=[corrupt, BODY])

        outcome = self.run_download(opener, lease=make_lease(max_attempts=2))

        self.assertEqual(outcome["status"], OUTCOME_SUCCESS, outcome)
        self.assertEqual([offset for _, offset, _ in opener.calls], [0, 0])
        self.assertEqual(self.saved(), BODY)

    def test_a_source_shorter_than_the_task_declared_fails_the_size_check(self) -> None:
        """The one input the size check is the only check that refuses.

        A short body that is also the wrong bytes is refused by the digest, and a
        body with a `Content-Length` is refused before it is read -- so neither of
        those pins the size check, and an implementation that dropped it would go
        green on both. Here nothing is declared up front and the digest the task
        carries is the digest of exactly what arrives, so the delivered count is
        the only thing that disagrees with the task.

        Which check spoke is asserted on the message, not on the code: both
        endings are `download_integrity_failed`, and the sentence is what tells
        the operator whether the file was the wrong size or the wrong content.
        """
        partial = BODY[: len(BODY) - CHUNK]
        lease = make_lease(body=partial, total_bytes=len(BODY))

        outcome = self.run_download(ServingOpener(partial, with_length=False), lease=lease)

        self.assertEqual(outcome["error_code"], ERROR_INTEGRITY_FAILED)
        self.assertEqual(
            self.completion()["error_message"],
            f"the source delivered {len(partial)} bytes where the task declared {len(BODY)}",
        )
        self.assertFalse((self.directory / FILE_NAME).exists())

    def test_a_source_whose_total_disagrees_is_refused_before_the_body_is_read(self) -> None:
        """`Content-Range` names an object of another size: stop before it lands."""
        self.plant_part(BODY[:CHUNK])
        opener = ServingOpener(content_range_total=len(BODY) + 4096)

        outcome = self.run_download(opener, resume=TransferResume(FILE_NAME, CHUNK))

        self.assertEqual(outcome["error_code"], ERROR_INTEGRITY_FAILED)
        self.assertEqual(
            len(self.part_bytes()),
            CHUNK,
            "a body that is the wrong object must not be appended to at all",
        )

    def test_a_source_longer_than_declared_is_stopped_at_the_crossing_chunk(self) -> None:
        """No `Content-Length`, so only the reading loop can notice.

        With a length header the disagreement is rejectable at the open; a
        chunked response says nothing up front, and the check that the bytes do
        not run past what the task declared is the last thing standing between
        the operator and a file that is longer than its own digest covers.

        What the check buys over the one after the loop is where it stops: the
        crossing chunk is the last one written, so the part is three chunks of a
        body that holds four. Removing it leaves the same error code behind -- the
        check at the end refuses the file either way -- but only after the whole
        body has been written to the operator's disk, so the part's length is
        what this arm reads.
        """
        lease = make_lease(total_bytes=CHUNK * 2)

        outcome = self.run_download(ServingOpener(with_length=False), lease=lease)

        self.assertEqual(outcome["error_code"], ERROR_INTEGRITY_FAILED)
        self.assertEqual(len(self.part_bytes()), CHUNK * 3)
        self.assertFalse((self.directory / FILE_NAME).exists())


class VolumeFailureTest(DownloadTest):
    """The volume failing *while* the download runs, which no check can foresee.

    A check of the free space is a reading of one moment, and the download is
    everything after it. These arms reach the failure the way production does --
    through the sink, with the errno the kernel would use -- because the mapping
    from an errno to one of the eight frozen codes is the whole of the handling.
    """

    def _failing_sink(self, error: OSError, method: str = "append") -> None:
        patcher = mock.patch.object(DownloadSink, method, side_effect=error)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_volume_that_fills_during_the_download_is_written_down_as_no_room(self) -> None:
        self._failing_sink(OSError(errno.ENOSPC, "No space left on device"))

        outcome = self.run_download(ServingOpener())

        self.assertEqual(outcome["status"], OUTCOME_FAILED)
        self.assertEqual(outcome["error_code"], ERROR_DISK_INSUFFICIENT)
        self.assertFalse((self.directory / FILE_NAME).exists())

    def test_a_directory_that_turns_unwritable_during_the_download_says_so(self) -> None:
        self._failing_sink(OSError(errno.EACCES, "Permission denied"))

        outcome = self.run_download(ServingOpener())

        self.assertEqual(outcome["error_code"], ERROR_SAVE_DIR_UNWRITABLE)

    def test_a_failure_of_the_rename_is_the_same_two_codes(self) -> None:
        """`commit` is the last thing between the bytes and the operator.

        A rename that cannot happen leaves the part a part, so the file is
        absent and the reason is the volume's -- not "the download failed".
        """
        self._failing_sink(OSError(errno.ENOSPC, "No space left on device"), method="commit")

        outcome = self.run_download(ServingOpener())

        self.assertEqual(outcome["error_code"], ERROR_DISK_INSUFFICIENT)
        self.assertFalse((self.directory / FILE_NAME).exists())
        self.assertTrue(DownloadSink(self.directory).part_path(TASK_ID).exists())

    def test_an_errno_nobody_has_decided_the_meaning_of_is_not_filed_under_one(self) -> None:
        """A wrong reason in front of the operator is worse than a raw failure.

        It raises, which is the runner's `executor_error`: the task still fails,
        and nothing has claimed to know why.
        """
        self._failing_sink(OSError(errno.EIO, "Input/output error"))

        with self.assertRaises(OSError):
            self.run_download(ServingOpener())
        self.assertEqual(self.cloud.completions, [])

    def test_the_message_of_a_volume_failure_does_not_carry_the_path(self) -> None:
        """`OSError.__str__` names the file it was opening, and this must not."""
        self._failing_sink(
            OSError(errno.ENOSPC, "No space left on device", str(self.directory / FILE_NAME))
        )

        self.run_download(ServingOpener())

        message = self.completion()["error_message"]  # type: ignore[assignment]
        self.assertEqual(message, "the chosen save directory's volume has no room for the file")
        self.assertNotIn(str(self.directory), message)


class ResumeTest(DownloadTest):
    def test_a_resumed_attempt_asks_for_the_byte_it_stopped_at(self) -> None:
        """And hashes what it already had, or the digest would be of the tail.

        The digest is the arm: the part holds the first chunk, the server sends
        the rest, and a digest started at the resume point would not match the
        task's. Only hashing both halves passes the check.
        """
        self.plant_part(BODY[:CHUNK])
        opener = ServingOpener()

        outcome = self.run_download(opener, resume=TransferResume(FILE_NAME, CHUNK))

        self.assertEqual(outcome["status"], OUTCOME_SUCCESS)
        self.assertEqual(opener.calls[0][1], CHUNK)
        self.assertEqual(self.saved(), BODY)

    def test_a_retry_carries_the_range_the_stall_left_it_at(self) -> None:
        self.plant_part(BODY[:CHUNK])
        opener = ServingOpener(stalls=[CHUNK])

        outcome = self.run_download(
            opener, lease=make_lease(max_attempts=2), resume=TransferResume(FILE_NAME, CHUNK)
        )

        self.assertEqual(outcome["status"], OUTCOME_SUCCESS)
        self.assertEqual([offset for _, offset, _ in opener.calls], [CHUNK, CHUNK * 2])

    def test_a_resumed_download_whose_record_is_behind_the_part_still_works(self) -> None:
        """The two may disagree, and the part on disk is the authority.

        A checkpoint can be a little behind -- it is written on a throttle -- and
        an attempt that asked the server to continue from the *record* would
        re-fetch bytes it already had. Reading the part's size costs one `stat`.
        """
        self.plant_part(BODY[:CHUNK])
        opener = ServingOpener()

        self.run_download(opener, resume=TransferResume(FILE_NAME, 0))

        self.assertEqual(opener.calls[0][1], CHUNK)
        self.assertEqual(self.saved(), BODY)

    def test_a_report_never_goes_backwards_past_what_was_already_reported(self) -> None:
        """The record exists so a restart does not show the operator a rewind.

        The part holds less than the count the last attempt reported -- an
        unflushed tail, a killed process -- and every figure Cloud is given has to
        be at least the one it already has. What holds that is the condition on
        the report itself: a report is due only once the count has grown past what
        was last said, so the first figure of a resumed attempt is the first one
        above the floor rather than the part's size. The figures here are 4096 and
        not 1024/2048/3072, and an implementation that dropped the growth
        condition would send the smaller ones on the first window boundary.
        """
        self.plant_part(BODY[:CHUNK])
        # A floor just below the body, so the catch-up stretch is most of the
        # download and the figures that would be too small are the interesting
        # ones -- the last chunk is what crosses the floor.
        resume = TransferResume(FILE_NAME, len(BODY) - 1)

        self.run_download(ServingOpener(), lease=make_lease(lease_seconds=600), resume=resume)

        reported = [completed for _, _, completed, _ in self.cloud.progress]
        self.assertTrue(reported, "the growth condition must not silence progress entirely")
        for completed in reported:
            self.assertGreaterEqual(completed, len(BODY) - 1)

    def test_the_record_carries_the_name_and_the_figure_cloud_was_given(self) -> None:
        self.run_download(ServingOpener())

        self.assertTrue(self.recorded)
        for record in self.recorded:
            with self.subTest(record=record):
                self.assertEqual(record.file_name, FILE_NAME)
                self.assertGreater(record.bytes_done, 0)
        figures = [completed for _, _, completed, _ in self.cloud.progress]
        self.assertEqual([record.bytes_done for record in self.recorded], figures)

    def test_the_record_carries_a_name_and_not_a_path(self) -> None:
        """It is stored in a database, and CHG-061 §8 keeps paths out of those."""
        self.run_download(ServingOpener())

        for record in self.recorded:
            self.assertNotIn("/", record.file_name)


class ProgressTest(DownloadTest):
    def test_a_report_carries_the_window_s_rate_and_not_an_average(self) -> None:
        """The desktop shows this as "how fast is this going", which is about now.

        Each chunk arrives one clock tick after the last and every window is the
        same size, so a per-window rate is the same number each time and a
        running average would fall off -- 1024, 512, 341, 256.
        """
        self.run_download(ServingOpener())

        rates = [rate for _, _, _, rate in self.cloud.progress]
        self.assertGreater(len(rates), 1, "this arm needs more than one window")
        self.assertEqual(set(rates), {CHUNK})

    def test_every_report_is_at_least_the_two_hundredth_of_the_file(self) -> None:
        """A small file still reports a few times; a large one is not noisy."""
        body = bytes(range(256)) * 400
        self.run_download(ServingOpener(body), lease=make_lease(body))

        self.assertLessEqual(len(self.cloud.progress), len(body) // CHUNK + 1)

    def test_a_heartbeat_is_counted_on_the_clock_and_not_on_the_progress(self) -> None:
        """A note that reports nothing must still renew the lease.

        The throttle is set so that no progress report is ever due; the heartbeats
        must arrive anyway, because the lease is renewed by time and not by bytes.
        """
        self.run_download(
            ServingOpener(),
            lease=make_lease(lease_seconds=3),
            progress_min_bytes=1 << 40,
            progress_min_seconds=1 << 40,
        )

        self.assertEqual(self.cloud.progress, [])
        self.assertTrue(self.cloud.heartbeats)
        for credential, task_id, _ in self.cloud.heartbeats:
            self.assertEqual((credential, task_id), (CREDENTIAL, TASK_ID))

    def test_a_heartbeat_carries_the_byte_figure_and_the_lease_it_renews(self) -> None:
        self.run_download(ServingOpener(), lease=make_lease(lease_seconds=3))

        figures = [completed for _, _, completed in self.cloud.heartbeats]
        self.assertTrue(figures)
        self.assertTrue(all(0 < figure <= len(BODY) for figure in figures), figures)

    def test_a_heartbeat_during_the_catch_up_does_not_go_backwards(self) -> None:
        """The heartbeat is the one call that can arrive below the floor.

        A resumed attempt whose part is shorter than the count the last attempt
        reported spends its first stretch with nothing to report, and the
        heartbeat fires anyway -- it is counted on the clock, which is why it
        cannot be moved behind the same condition the progress report uses. So it
        is the call that would tell Cloud the download went backwards, during the
        stretch where the record is the only thing that says otherwise. The figure
        it carries is the floor, and the desktop shows it unchanged.

        The lease is short on purpose: with the default the interval is longer
        than the whole catch-up stretch and the heartbeat would never fire here.
        """
        self.plant_part(BODY[:CHUNK])
        resume = TransferResume(FILE_NAME, len(BODY) - 1)

        self.run_download(ServingOpener(), lease=make_lease(lease_seconds=3), resume=resume)

        figures = [completed for _, _, completed in self.cloud.heartbeats]
        self.assertTrue(figures, "the lease must be renewed during the catch-up")
        for figure in figures:
            self.assertGreaterEqual(figure, len(BODY) - 1)


class StallTest(DownloadTest):
    def test_a_stall_is_retried_from_where_it_stopped(self) -> None:
        """A dropped connection costs the rest of the file, not the whole file."""
        opener = ServingOpener(stalls=[CHUNK])

        outcome = self.run_download(opener, lease=make_lease(max_attempts=2))

        self.assertEqual(outcome["status"], OUTCOME_SUCCESS)
        self.assertEqual([offset for _, offset, _ in opener.calls], [0, CHUNK])
        self.assertEqual(self.saved(), BODY)
        self.assertEqual(self.completion()["status"], "success")

    def test_a_stall_that_survives_every_attempt_is_written_down(self) -> None:
        opener = ServingOpener(stalls=[CHUNK, CHUNK])

        outcome = self.run_download(opener, lease=make_lease(max_attempts=2))

        self.assertEqual(outcome["status"], OUTCOME_FAILED)
        self.assertEqual(outcome["error_code"], ERROR_STALLED)
        self.assertEqual(self.completion()["status"], "failed")
        self.assertFalse((self.directory / FILE_NAME).exists())

    def test_a_stall_pauses_before_the_next_attempt(self) -> None:
        self.run_download(ServingOpener(stalls=[CHUNK]), lease=make_lease(max_attempts=2))

        self.assertEqual(len(self.sleeps), 1)

    def test_the_budget_is_what_the_lease_says_is_left(self) -> None:
        """`max_attempts` is the task's total, `attempt_count` what it has spent."""
        opener = ServingOpener(stalls=[CHUNK, CHUNK, CHUNK])

        outcome = self.run_download(
            opener, lease=make_lease(max_attempts=3, attempt_count=2)
        )

        self.assertEqual(len(opener.calls), 1, "one attempt was left, not three")
        self.assertEqual(outcome["error_code"], ERROR_STALLED)

    def test_a_lease_with_no_budget_left_still_gets_one_attempt(self) -> None:
        opener = ServingOpener()

        outcome = self.run_download(
            opener, lease=make_lease(max_attempts=2, attempt_count=2)
        )

        self.assertEqual(outcome["status"], OUTCOME_SUCCESS)
        self.assertEqual(len(opener.calls), 1)

    def test_an_expired_address_is_not_retried(self) -> None:
        """A re-issued address is Cloud's to give; the same one stays dead.

        The arm is the call count: with a budget of three, an implementation that
        retried an unavailable source would ask the dead address three times.
        """
        opener = ServingOpener(
            raises=urlerror.HTTPError(SIGNED_URL, 403, "Forbidden", {}, None)
        )

        outcome = self.run_download(opener, lease=make_lease(max_attempts=3))

        self.assertEqual(outcome["error_code"], ERROR_SOURCE_UNAVAILABLE)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_an_unreachable_host_is_an_unavailable_source_too(self) -> None:
        opener = ServingOpener(raises=urlerror.URLError(OSError("network is down")))

        outcome = self.run_download(opener, lease=make_lease(max_attempts=3))

        self.assertEqual(outcome["error_code"], ERROR_SOURCE_UNAVAILABLE)


class RangeIgnoredTest(DownloadTest):
    def test_a_server_that_ignores_the_range_is_not_appended_to(self) -> None:
        """The fault this guards against produces a file of the right length.

        A `200` to a ranged request is the whole object. Appending it to the part
        an earlier attempt left builds a file of exactly the declared size out of
        two overlapping copies of the object -- so the size check passes, and
        only the digest notices, hours later and far from the cause.
        """
        self.plant_part(BODY[:CHUNK])
        opener = ServingOpener(honour_range=False)

        outcome = self.run_download(opener, resume=TransferResume(FILE_NAME, CHUNK))

        self.assertEqual(outcome["status"], OUTCOME_SUCCESS)
        self.assertEqual(self.saved(), BODY)
        self.assertEqual(sha256(self.saved()), sha256(BODY))

    def test_the_range_that_was_not_honoured_is_asked_for_again_from_zero(self) -> None:
        self.plant_part(BODY[:CHUNK])
        opener = ServingOpener(honour_range=False)

        self.run_download(opener, resume=TransferResume(FILE_NAME, CHUNK))

        self.assertEqual(opener.calls[0][1], CHUNK, "the request offered to resume")
        self.assertEqual(len(opener.calls), 1, "and was not re-issued at the server")


class CloudTroubleTest(DownloadTest):
    def test_a_lost_lease_stops_the_download_and_reports_nothing(self) -> None:
        """A `409` means somebody else owns the task; there is nothing to report."""
        self.cloud.raise_on["heartbeat"] = TransferLeaseLostError("gone")

        outcome = self.run_download(ServingOpener(), lease=make_lease(lease_seconds=3))

        self.assertEqual(outcome["status"], OUTCOME_LEASE_LOST)
        self.assertEqual(self.cloud.completions, [])
        self.assertFalse((self.directory / FILE_NAME).exists())
        self.assertTrue(
            DownloadSink(self.directory).part_path(TASK_ID).exists(),
            "the bytes stay: a re-issued task is a head start, and the digest "
            "checks them either way",
        )

    def test_a_lost_lease_at_completion_is_not_a_failure_of_the_download(self) -> None:
        """The bytes are on disk; what went wrong is Cloud's answer about them."""
        self.cloud.raise_on["complete"] = TransferLeaseLostError("gone")

        outcome = self.run_download(ServingOpener())

        self.assertEqual(outcome["status"], OUTCOME_LEASE_LOST)
        self.assertEqual(self.saved(), BODY)

    def test_a_rejected_completion_names_the_integrity_code(self) -> None:
        self.cloud.raise_on["complete"] = TransferIntegrityRejectedError("bad bytes")

        outcome = self.run_download(ServingOpener())

        self.assertEqual(outcome["status"], OUTCOME_REJECTED)
        self.assertEqual(outcome["error_code"], ERROR_INTEGRITY_FAILED)
        self.assertEqual(self.saved(), BODY)

    def test_a_cloud_that_cannot_be_reached_leaves_the_file_and_says_so(self) -> None:
        """"Finished here, unknown there" is its own outcome, not a failure."""
        self.cloud.raise_on["complete"] = TransferUnavailableError("HTTP 503")

        outcome = self.run_download(ServingOpener())

        self.assertEqual(outcome["status"], OUTCOME_UNREPORTED)
        self.assertEqual(outcome["error_code"], ERROR_LEASE_UNCONFIRMED)
        self.assertEqual(self.saved(), BODY)

    def test_a_cloud_5xx_during_the_download_is_retried(self) -> None:
        self.cloud.raise_on["progress"] = TransferUnavailableError("HTTP 503")
        opener = ServingOpener()

        outcome = self.run_download(opener, lease=make_lease(max_attempts=2))

        self.assertEqual(outcome["status"], OUTCOME_SUCCESS)
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(self.saved(), BODY)

    def test_a_cloud_5xx_that_outlasts_the_budget_is_not_a_cancellation(self) -> None:
        """The two refusals name different operator actions, so they differ here.

        A `5xx` is transient and ends as `lease_unconfirmed` -- the task may
        belong to another owner -- while a `409` ends the download outright. An
        implementation that read both as "cancelled" would stop downloading for a
        server that was merely restarting.
        """
        self.cloud.raise_on["progress"] = TransferUnavailableError("HTTP 503")
        self.cloud.raise_on["heartbeat"] = TransferUnavailableError("HTTP 503")
        opener = ServingOpener()

        outcome = self.run_download(
            opener,
            lease=make_lease(max_attempts=2, lease_seconds=1),
            progress_min_bytes=1,
            progress_min_seconds=0.0,
        )

        self.assertEqual(outcome["status"], OUTCOME_FAILED)
        self.assertEqual(outcome["error_code"], ERROR_LEASE_UNCONFIRMED)
        self.assertNotEqual(outcome["error_code"], ERROR_STALLED)


class ContractVocabularyTest(unittest.TestCase):
    def test_every_code_it_can_write_down_is_in_the_contract(self) -> None:
        """The codes are literals in the module and keys in a frozen file.

        Compared in both directions: a code the executor can emit that the
        contract does not name is a reason Cloud cannot classify, and a contract
        name nothing emits is a reason an operator will never see.
        """
        text = CONTRACT.read_text(encoding="utf-8")
        block = text.split("executor_errors:", 1)[1].split("secret_policy:", 1)[0]
        named = set(re.findall(r"^  ([a-z_]+):", block, re.MULTILINE))

        self.assertGreaterEqual(len(named), 8, f"the contract was read as {sorted(named)}")
        self.assertEqual(named, set(EXECUTOR_ERROR_CODES))

    def test_the_reader_can_see_a_code_that_is_not_there(self) -> None:
        """Without this, a reader that returned nothing would pass the arm above."""
        text = CONTRACT.read_text(encoding="utf-8")
        block = text.split("executor_errors:", 1)[1].split("secret_policy:", 1)[0]
        named = set(re.findall(r"^  ([a-z_]+):", block, re.MULTILINE))

        self.assertIn("download_stalled", named)
        self.assertNotIn("download_made_up", named)


if __name__ == "__main__":
    unittest.main()
