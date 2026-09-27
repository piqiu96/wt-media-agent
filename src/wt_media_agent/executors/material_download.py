"""Download one leased material into the operator's chosen directory.

The bytes come from a short-lived signed CDN address, land in a `.part` file
inside the save directory, and reach their final name only after the size the
task declared and the digest it declared both hold. That last clause is the whole
of this module's correctness: a download that is interrupted, resumed, restarted
or killed leaves either a `.part` that is plainly not the file, or a file whose
bytes hash to what Cloud said. Nothing else is allowed to exist, because the
desktop will offer to open whatever is there and the operator cannot tell a
truncated video from a short one.

**What this module does not do.** It does not open a socket (`clients/transfer/`
does), does not write to a path (`storage/download_sink.py` does), and does not
write a checkpoint (the runner owns that table per ADR-0016 §2, so it is handed
the durable record instead). ADR-0016 §3 denies executors `os`, `http`, `urllib`
and `socket` outright, and every one of those denials is a seam here rather than
an inconvenience: the address is in `clients/`, the filesystem is in `storage/`,
and the clock is a constructor argument.

**The four Cloud calls.** A download is a lease that must be renewed
(`heartbeat`), a progress figure that must be reported (`progress`), and one
terminal report when it ends (`complete`). The credential is those calls' whole
identity and this module never puts it anywhere else -- not in a message, not in
a log line, not in the record it hands the runner.

**Two facts are deliberately never written down or logged**: the save directory
(a local absolute path, and CHG-061 §8 keeps those out of everything this Agent
records or reports) and the address (a short-lived credential, named in
`contracts/local-error-codes/v1/transfer.yaml`'s `secret_policy.forbidden_fields`).
Every message this module composes is a fixed string about a code, a count or a
file name, so there is no path and no URL for one to carry.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import time
from collections.abc import Mapping
from typing import Callable, Optional

from wt_media_agent.clients.cloud import (
    CloudAgentClient,
    TransferIntegrityRejectedError,
    TransferLease,
    TransferLeaseLostError,
    TransferUnavailableError,
)
from wt_media_agent.clients.transfer import (
    Opener,
    SourceStalledError,
    SourceUnavailableError,
    extension_from_url,
    open_source,
)
from wt_media_agent.storage import (
    DownloadSink,
    InsufficientSpaceError,
    NameUnusableError,
    TransferResume,
)

logger = logging.getLogger(__name__)

#: The terminal reasons this executor may write down: exactly the keys of
#: `contracts/local-error-codes/v1/transfer.yaml`'s `executor_errors`, and
#: nothing else. They are literals here rather than loaded from the YAML because
#: the file is a contract with the reader, not a runtime dependency -- the test
#: that compares this tuple against the contract is what keeps the two honest,
#: in both directions.
ERROR_SAVE_DIR_UNSET = "download_save_dir_unset"
ERROR_SAVE_DIR_UNWRITABLE = "download_save_dir_unwritable"
ERROR_DISK_INSUFFICIENT = "download_disk_insufficient"
ERROR_SOURCE_UNAVAILABLE = "download_source_unavailable"
ERROR_INTEGRITY_FAILED = "download_integrity_failed"
ERROR_STALLED = "download_stalled"
ERROR_LEASE_UNCONFIRMED = "lease_unconfirmed"
ERROR_NAME_UNUSABLE = "download_name_unusable"

EXECUTOR_ERROR_CODES = (
    ERROR_SAVE_DIR_UNSET,
    ERROR_SAVE_DIR_UNWRITABLE,
    ERROR_DISK_INSUFFICIENT,
    ERROR_SOURCE_UNAVAILABLE,
    ERROR_INTEGRITY_FAILED,
    ERROR_STALLED,
    ERROR_LEASE_UNCONFIRMED,
    ERROR_NAME_UNUSABLE,
)

#: What `execute` returns in its `status` field. The first four are outcomes of
#: the download; `unreported` is the one where the file is on this machine and
#: Cloud does not know -- the runner must not treat that as an end.
OUTCOME_SUCCESS = "success"
OUTCOME_FAILED = "failed"
OUTCOME_LEASE_LOST = "lease_lost"
OUTCOME_REJECTED = "rejected"
OUTCOME_UNREPORTED = "unreported"

#: Bytes read from the source at a time. Sized for a CDN read, not for a disk
#: write: it bounds how much a stall can cost, and it is the granularity the
#: progress throttle sees.
DEFAULT_CHUNK_BYTES = 256 * 1024

#: Seconds without a byte before the source counts as stalled. It is handed to
#: the socket as its timeout rather than checked against a wall clock, so the
#: window is about the transfer and not about how long the process has been up.
DEFAULT_STALL_SECONDS = 30.0

#: A progress report goes out when either bound is reached first: this many bytes
#: since the last report, or this many seconds since it. The byte bound is a
#: floor -- the real one is at least a two-hundredth of the file, so a small file
#: still reports a few times and a large one does not send a report per megabyte.
DEFAULT_PROGRESS_MIN_BYTES = 1024 * 1024
DEFAULT_PROGRESS_MIN_SECONDS = 2.0
DEFAULT_PROGRESS_FRACTION = 200

#: Pause before a retried attempt. It is not a backoff curve: the retries are
#: bounded by the lease's own `max_attempts`, and the thing being waited out is a
#: connection that just died, not a service under load.
DEFAULT_RETRY_PAUSE_SECONDS = 1.0

#: The two sentences a volume fault is written down with. Constants rather than
#: literals at each site because they are the same fault whether it is found by
#: the check before the download or by an `OSError` during it, and because this
#: module's messages are the only thing that reaches `error_message`: an
#: `OSError`'s own text carries the path it was opening, which is exactly what
#: must not travel.
NO_ROOM_MESSAGE = "the chosen save directory's volume has no room for the file"
NOT_WRITABLE_MESSAGE = "the chosen save directory is no longer writable"

#: What the runner hands the executor to record, and the executor's own report.
#:
#: `Recorder` is how the durable state leaves this module: the executor knows
#: what to write down (a name and a byte count) and the runner owns where it goes.
Recorder = Callable[[TransferResume], None]


class DownloadFault(RuntimeError):
    """A way a download ended, carrying the code Cloud will be told.

    One type per cause rather than one type with a code argument, because the
    retry decision reads off the type: an address that has expired is not retried
    and a connection that died is, and a subclass is the one spelling of that
    which cannot be got wrong at a call site.

    The subclasses declare three class attributes and nothing else: the message
    is the exception's own argument, so there is one spelling of it rather than
    one in `args` and another beside it.
    """

    code = ""
    #: Retried within the attempt budget before the code is written down.
    retryable = False
    #: The part file is unusable and must go before the next attempt. Only
    #: integrity: a stall resumes from what is on disk, a bad hash does not.
    discard = False


class SaveDirectoryUnset(DownloadFault):
    code = ERROR_SAVE_DIR_UNSET


class SaveDirectoryUnwritable(DownloadFault):
    code = ERROR_SAVE_DIR_UNWRITABLE


class DiskInsufficient(DownloadFault):
    code = ERROR_DISK_INSUFFICIENT


class UnusableName(DownloadFault):
    code = ERROR_NAME_UNUSABLE


class SourceUnavailable(DownloadFault):
    code = ERROR_SOURCE_UNAVAILABLE


class Stalled(DownloadFault):
    code = ERROR_STALLED
    retryable = True


class Integrity(DownloadFault):
    code = ERROR_INTEGRITY_FAILED
    retryable = True
    discard = True


class LeaseUnconfirmed(DownloadFault):
    code = ERROR_LEASE_UNCONFIRMED
    retryable = True


class MaterialDownloadExecutor:
    """Download one leased material, and say what Cloud should be told."""

    def __init__(
        self,
        client: CloudAgentClient,
        agent_id: str,
        *,
        credential: str,
        save_directory: Callable[[], Optional[str]],
        record: Recorder,
        opener: Optional[Opener] = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
        stall_seconds: float = DEFAULT_STALL_SECONDS,
        progress_min_bytes: int = DEFAULT_PROGRESS_MIN_BYTES,
        progress_min_seconds: float = DEFAULT_PROGRESS_MIN_SECONDS,
    ) -> None:
        self.client = client
        self.agent_id = agent_id
        self._credential = credential
        # Called once per attempt, not once per executor: the operator may change
        # the directory while a download is running, and the resume record
        # deliberately holds no path so that the change costs them nothing.
        self._save_directory = save_directory
        self._record = record
        self._opener = opener
        self._clock = clock
        self._sleep = sleeper
        self._chunk_bytes = chunk_bytes
        self._stall_seconds = stall_seconds
        self._progress_min_bytes = progress_min_bytes
        self._progress_min_seconds = progress_min_seconds

    # ---- the one entry point ----

    def execute(self, task: Mapping[str, object]) -> Mapping[str, object]:
        """Download the lease in `task["lease"]`, resuming `task["resume"]` if given.

        Returns what happened, and never raises for a download that failed: the
        caller is a loop that has other work, and an exception here would say
        "this executor is broken" about a download whose cause is in the world
        (an expired address, a full disk, a lease somebody else took). The two
        exceptions are the ones that are not about this download at all:
        `SessionInvalidError` (the node credential is refused, so nothing this
        loop does next can work) and `ValueError` (the caller handed over
        something that is not a lease, which is a fault in this process).
        """
        lease = _lease_of(task)
        resume = _resume_of(task)
        try:
            sink, name = self._prepare(lease, resume)
        except DownloadFault as fault:
            # Nothing was written, so there is no byte count to report.
            return self._report_failure(lease, None, fault)

        try:
            written, digest = self._attempts(lease, sink, name, resume)
        except TransferLeaseLostError:
            # Cloud has already reached a terminal state for this task -- it was
            # cancelled, taken over, or finished. There is nothing to report and
            # nothing to retry, and the bytes on disk are left where they are:
            # if Cloud re-issues the task to this same node the part is a head
            # start, and the size and the digest check it either way.
            logger.warning(
                "material download lost its lease before it finished: %s",
                lease.task_id,
                extra={"task_id": lease.task_id, "error_code": ERROR_LEASE_UNCONFIRMED},
            )
            return _outcome(lease, OUTCOME_LEASE_LOST, error_code=ERROR_LEASE_UNCONFIRMED)
        except DownloadFault as fault:
            return self._report_failure(lease, sink, fault)

        return self._commit(lease, sink, name, written, digest)

    # ---- setup ----

    def _prepare(
        self, lease: TransferLease, resume: Optional[TransferResume]
    ) -> tuple[DownloadSink, str]:
        """The sink to write into and the name to write, or the fault that stops it."""
        directory = self._save_directory()
        if not directory:
            # Not a fallback to the system temp directory: a file the operator
            # cannot find is not a file they asked for, and quietly keeping 4 GB
            # of video somewhere they did not choose is worse than saying so.
            raise SaveDirectoryUnset("no save directory has been chosen on this machine")
        sink = DownloadSink(directory)
        if not sink.is_writable():
            raise SaveDirectoryUnwritable(NOT_WRITABLE_MESSAGE)
        try:
            # A resumed download keeps the name the earlier attempt chose.
            # Re-deriving it is not the same answer: `allocate` picks the first
            # name that is free *now*, so a second attempt racing another
            # download onto the same title would file one download as two.
            name = (
                resume.file_name
                if resume is not None
                else sink.file_name(
                    lease.title, lease.asset_id, extension_from_url(lease.download_url)
                )
            )
            name = sink.allocate(name)
        except NameUnusableError:
            raise UnusableName("the material's title leaves no usable file name") from None
        try:
            sink.require_room(lease.total_bytes)
        except InsufficientSpaceError:
            raise DiskInsufficient(NO_ROOM_MESSAGE) from None
        return sink, name

    # ---- the attempt loop ----

    def _attempts(
        self,
        lease: TransferLease,
        sink: DownloadSink,
        name: str,
        resume: Optional[TransferResume],
    ) -> tuple[int, str]:
        """Transfer the file, retrying what the lease's budget allows.

        The budget is the contract's: `max_attempts` is how many attempts Cloud
        will let this task have *in total*, and `attempt_count` is how many it has
        already had before this lease, so what is left is the difference. One
        attempt is always made even when the numbers say none is left -- a lease
        that arrives with the budget spent is still a lease somebody is holding.
        """
        remaining = max(1, lease.max_attempts - lease.attempt_count)
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._attempt(lease, sink, name, resume)
            except DownloadFault as fault:
                if not fault.retryable or attempt >= remaining:
                    raise
                if fault.discard:
                    sink.discard(lease.task_id)
                logger.info(
                    "material download attempt %d/%d failed for %s: %s",
                    attempt,
                    remaining,
                    lease.task_id,
                    fault.code,
                    extra={"task_id": lease.task_id, "error_code": fault.code},
                )
                self._sleep(DEFAULT_RETRY_PAUSE_SECONDS)

    def _attempt(
        self,
        lease: TransferLease,
        sink: DownloadSink,
        name: str,
        resume: Optional[TransferResume],
    ) -> tuple[int, str]:
        """One attempt, with the world's errors turned into this module's.

        The conversion happens here, and in one place, because it is the same
        decision three times: which of the world's failures this download may
        retry. It is not a `try` around the retry loop's own decision -- a fault
        raised inside the `except` that decides would leave the loop rather than
        go round it. `SessionInvalidError` is deliberately not in the list: a
        refused node credential is not something a retry can outlast, and it is
        the runner's to act on.
        """
        try:
            return self._transfer(lease, sink, name, resume)
        except SourceUnavailableError as exc:
            # The address was refused or has expired. Retrying the same address
            # would ask the same dead question, so this is not retryable: Cloud
            # issues a new one.
            raise SourceUnavailable(str(exc)) from None
        except SourceStalledError as exc:
            raise Stalled(str(exc)) from None
        except TransferUnavailableError as exc:
            raise LeaseUnconfirmed(str(exc)) from None
        except OSError as exc:
            # The volume failed while the download was running -- the case the
            # check before it cannot see, because the check is a moment and this
            # is everything after it. An errno nothing here names is re-raised
            # rather than filed under a reason that might not be true.
            fault = _fault_for_oserror(exc)
            if fault is None:
                raise
            raise fault from None

    def _transfer(
        self,
        lease: TransferLease,
        sink: DownloadSink,
        name: str,
        resume: Optional[TransferResume],
    ) -> tuple[int, str]:
        """One attempt: open, stream, and check the bytes before anyone else sees them.

        The digest is seeded from the part file when there is one, because the
        check at the end is over the whole file and a resumed attempt only has the
        tail in hand. The offset itself comes from the part file's size and never
        from the resume record: a stored count can be ahead of the bytes actually
        on disk, and asking the server to continue from it would skip exactly the
        lost bytes and produce a file of the right length and the wrong content.
        """
        offset = sink.resume_offset(lease.task_id)
        digest = hashlib.sha256()
        if offset:
            for chunk in sink.part_chunks(lease.task_id):
                digest.update(chunk)

        progress = _Progress(
            client=self.client,
            credential=self._credential,
            record=self._record,
            task_id=lease.task_id,
            name=name,
            total_bytes=lease.total_bytes,
            already_reported=resume.bytes_done if resume is not None else 0,
            lease_seconds=lease.lease_seconds,
            clock=self._clock,
            min_bytes=self._progress_min_bytes,
            min_seconds=self._progress_min_seconds,
        )

        with open_source(
            lease.download_url, offset, opener=self._opener, timeout=self._stall_seconds
        ) as stream:
            if not stream.range_honoured:
                # The server was asked for a range and sent the whole file. A
                # caller that appended it to the part would build a file of the
                # declared length out of two overlapping copies of itself, and the
                # digest at the end is the only thing that would notice.
                sink.discard(lease.task_id)
                offset = 0
                digest = hashlib.sha256()
                logger.info(
                    "the source ignored the range request for %s; restarting it",
                    lease.task_id,
                    extra={"task_id": lease.task_id},
                )
            if stream.total_bytes >= 0 and stream.total_bytes != lease.total_bytes:
                raise Integrity(
                    "the source is not the object the task declared: it is "
                    f"{stream.total_bytes} bytes where the task says {lease.total_bytes}"
                )

            written = offset
            while True:
                chunk = stream.read(self._chunk_bytes)
                if not chunk:
                    break
                written += sink.append(lease.task_id, chunk)
                digest.update(chunk)
                progress.note(written)
                if written > lease.total_bytes:
                    raise Integrity(
                        "the source delivered more bytes than the task declared"
                    )

        if written != lease.total_bytes:
            raise Integrity(
                f"the source delivered {written} bytes where the task declared "
                f"{lease.total_bytes}"
            )
        hexdigest = digest.hexdigest()
        if hexdigest != lease.expected_sha256.lower():
            raise Integrity("the bytes delivered do not match the digest the task declared")
        return written, hexdigest

    # ---- getting the result out ----

    def _commit(
        self, lease: TransferLease, sink: DownloadSink, name: str, written: int, digest: str
    ) -> Mapping[str, object]:
        """Rename the part onto its final name, then tell Cloud.

        The rename comes first and is conditional on nothing: by this point the
        size and the digest have both been checked against what the task
        declared, and a `.part` that has got this far is the file. What can still
        go wrong is Cloud's answer, and the three answers are three different
        facts: a `409` means the task ended elsewhere, a `422` means Cloud
        checked the same bytes again and disagreed, and anything else means the
        file is here and Cloud has not been told.
        """
        try:
            sink.commit(lease.task_id, name)
        except OSError as exc:
            # The bytes are all here and could not be put in their place. The
            # part stays a part -- `commit` is a rename, so a failure is a file
            # that was never created rather than a half-written one.
            fault = _fault_for_oserror(exc)
            if fault is None:
                raise
            return self._report_failure(lease, sink, fault)
        logger.info(
            "material download committed %d bytes for %s as %s",
            written,
            lease.task_id,
            name,
            extra={"task_id": lease.task_id},
        )
        try:
            self.client.complete_transfer_task(
                self._credential,
                lease.task_id,
                status="success",
                completed_bytes=written,
                sha256=digest,
                file_name=name,
            )
        except TransferLeaseLostError:
            return _outcome(
                lease, OUTCOME_LEASE_LOST, completed_bytes=written, file_name=name,
                error_code=ERROR_LEASE_UNCONFIRMED,
            )
        except TransferIntegrityRejectedError:
            return _outcome(
                lease, OUTCOME_REJECTED, completed_bytes=written, file_name=name,
                error_code=ERROR_INTEGRITY_FAILED,
            )
        except TransferUnavailableError as exc:
            logger.warning(
                "material download finished but Cloud could not be told: %s", exc,
                extra={"task_id": lease.task_id, "error_code": ERROR_LEASE_UNCONFIRMED},
            )
            return _outcome(
                lease, OUTCOME_UNREPORTED, completed_bytes=written, file_name=name,
                error_code=ERROR_LEASE_UNCONFIRMED,
            )
        return _outcome(lease, OUTCOME_SUCCESS, completed_bytes=written, file_name=name)

    def _report_failure(
        self, lease: TransferLease, sink: Optional[DownloadSink], fault: DownloadFault
    ) -> Mapping[str, object]:
        """Write the reason down on the task, and say whether it landed.

        How far it got is reported when it is knowable, because "stalled at 91%"
        and "stalled at 2%" are different operator problems. The reason itself is
        one of eight fixed sentences and never the world's: `error_message`
        travels to a Cloud table, and neither a path nor an address may go with
        it.
        """
        completed = sink.resume_offset(lease.task_id) if sink is not None else None
        logger.warning(
            "material download failed: %s %s (%s)",
            lease.task_id,
            fault.code,
            fault,
            extra={"task_id": lease.task_id, "error_code": fault.code},
        )
        try:
            self.client.complete_transfer_task(
                self._credential,
                lease.task_id,
                status="failed",
                completed_bytes=completed,
                error_code=fault.code,
                error_message=str(fault),
            )
        except TransferLeaseLostError:
            return _outcome(lease, OUTCOME_LEASE_LOST, error_code=fault.code)
        except (TransferUnavailableError, TransferIntegrityRejectedError) as exc:
            logger.warning(
                "material download failure could not be reported: %s", exc,
                extra={"task_id": lease.task_id, "error_code": fault.code},
            )
            return _outcome(
                lease, OUTCOME_UNREPORTED, completed_bytes=completed, error_code=fault.code
            )
        return _outcome(lease, OUTCOME_FAILED, completed_bytes=completed, error_code=fault.code)


class _Progress:
    """What one attempt has, what Cloud has been told, and when to say more.

    Two calls, because they answer different questions and neither implies the
    other. `progress` carries how much of the file is here and how fast it is
    arriving; `heartbeat` is what renews the lease. A download that has stalled
    reports no progress for minutes at a time and must still hold its lease, so
    the heartbeat is counted on the clock and not on the bytes.

    **The rate is a window's, not the download's.** It is measured between two
    reports, so it describes the last few seconds and not the average since the
    transfer started -- an average is permanently poisoned by the seconds before
    the connection was warm, and the desktop shows this number as "how fast is
    this going", which is a question about now.

    **A figure never goes backwards.** The count the last report carried is what
    the next one is measured against, and a report needs that count to have
    grown, so the sequence is increasing by construction. A resumed attempt whose
    part file is shorter than the count the last attempt reported spends its
    first stretch below the floor with nothing to say, which is the right answer:
    the operator was already told the larger figure and the file does hold the
    bytes it was told about.

    `_floor` is what `already_reported` was called at construction, and it is
    needed in exactly one place, `_heartbeat` -- see the note there for why the
    progress path does not need it and the heartbeat does.
    """

    def __init__(
        self,
        *,
        client: CloudAgentClient,
        credential: str,
        record: Recorder,
        task_id: str,
        name: str,
        total_bytes: int,
        already_reported: int,
        lease_seconds: int,
        clock: Callable[[], float],
        min_bytes: int,
        min_seconds: float,
    ) -> None:
        self._client = client
        self._credential = credential
        self._record = record
        self._task_id = task_id
        self._name = name
        self._floor = already_reported
        self._reported = already_reported
        # The byte bound is a floor, not the rule: a report every megabyte is a
        # hundred reports for a hundred megabytes, which reads as noise, so the
        # real bound is a two-hundredth of the file whenever that is larger.
        self._report_every = max(min_bytes, total_bytes // DEFAULT_PROGRESS_FRACTION)
        self._min_seconds = min_seconds
        # A third of the lease, so two heartbeats can be lost to a slow network
        # before the lease is actually at risk.
        self._heartbeat_every = max(1.0, lease_seconds / 3)
        self._clock = clock
        # One reading, not two: the two windows open together, and a second
        # reading would make the first window as long as the clock moved between
        # them -- which on an injected clock is one whole tick, and shows up as a
        # first rate that is half of every rate after it.
        now = clock()
        self._window_at = now
        self._heartbeat_at = now

    def note(self, written: int) -> None:
        """Record `written` bytes here, reporting to Cloud if it is time to.

        The heartbeat is not behind the progress condition, and that is the
        point: a note that reports nothing -- the bytes are inside the throttle,
        or the count is still below the floor a previous attempt already
        reported -- must still renew the lease. A download whose floor is ahead
        of its part file spends the first stretch with nothing to report, and
        that is exactly when the lease must not lapse.
        """
        now = self._clock()
        elapsed = max(now - self._window_at, 1e-6)
        grown = written - self._reported
        if grown > 0 and (grown >= self._report_every or elapsed >= self._min_seconds):
            self._send_progress(written, int(grown / elapsed))
            self._window_at = now
        self._heartbeat(now, written)

    def _send_progress(self, written: int, bytes_per_second: int) -> None:
        self._client.report_transfer_progress(
            self._credential, self._task_id, written, bytes_per_second
        )
        self._reported = written
        # The record is written where Cloud is told, and not more often: it exists
        # so a restart does not report a figure that goes backwards, which is
        # exactly the fact the last report established.
        self._record(TransferResume(self._name, written))

    def _heartbeat(self, now: float, written: int) -> None:
        """Renew the lease, carrying the count Cloud should still be showing.

        The clamp is here and not in `_send_progress` because this call is not
        behind the growth condition: it fires on the clock alone, so it is the
        one path that can reach Cloud during the stretch where a resumed attempt
        is still below what the last attempt reported. Without it, a download
        resuming from a part shorter than its record would send a figure that
        decreases -- the fact the record exists to prevent -- on the very
        heartbeat that renews its lease.
        """
        if (now - self._heartbeat_at) < self._heartbeat_every:
            return
        self._client.heartbeat_transfer_task(
            self._credential, self._task_id, max(written, self._floor)
        )
        self._heartbeat_at = now


def _fault_for_oserror(exc: OSError) -> Optional[DownloadFault]:
    """The fault an `OSError` names, or `None` when nothing here names it.

    The message is this module's and never the exception's: an `OSError` raised
    by `open()` carries the path it was opening, and `error_message` travels to a
    Cloud table where a local absolute path may not go (CHG-061 §8), so
    `str(exc)` is exactly the string that must not be used here.

    Only five errnos are mapped, and each is a fact about the volume rather than
    a guess: out of space or over quota is "no room", and a refusal by the
    permission bits, the filesystem, or the owner is "not writable". A sixth
    errno is a fault whose meaning nobody has decided, and filing it under one of
    these would put a reason in front of the operator that is not the reason.
    """
    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return DiskInsufficient(NO_ROOM_MESSAGE)
    if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        return SaveDirectoryUnwritable(NOT_WRITABLE_MESSAGE)
    return None


def _lease_of(task: Mapping[str, object]) -> TransferLease:
    """The lease this task carries, or a `ValueError` naming what was handed over."""
    lease = task.get("lease")
    if not isinstance(lease, TransferLease):
        raise ValueError("a material download needs a transfer lease under `lease`")
    return lease


def _resume_of(task: Mapping[str, object]) -> Optional[TransferResume]:
    """What the last attempt left to continue from, if anything."""
    resume = task.get("resume")
    if resume is None:
        return None
    if not isinstance(resume, TransferResume):
        raise ValueError("`resume` must be a TransferResume, or absent")
    return resume


def _outcome(
    lease: TransferLease,
    status: str,
    *,
    completed_bytes: Optional[int] = None,
    file_name: Optional[str] = None,
    error_code: Optional[str] = None,
) -> dict[str, object]:
    """What the runner is told, in one shape for every ending.

    `task_type` is not in it: this executor answers about a transfer, and the
    runner already knows which task type it dispatched on.
    """
    return {
        "task_id": lease.task_id,
        "status": status,
        "completed_bytes": completed_bytes,
        "file_name": file_name,
        "error_code": error_code,
    }
