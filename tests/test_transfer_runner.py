"""Tests for `TransferRunner`: the loop that runs downloads beside `TaskRunner`.

What is asserted here is the loop's own work, and it is deliberately narrow: it
claims, it hands the executor the lease and whatever record this machine holds
for it, and it decides what happens to that record afterwards. The download
itself is `test_material_download_executor.py`'s, and the fake executor below
answers instead of transferring -- so a green run here says nothing about bytes,
and everything about the row a re-issued task continues from.
"""

from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from wt_media_agent.clients.cloud import SessionInvalidError, TransferLease, TransferTerminal, TransferUnavailableError
from wt_media_agent.executors.material_download import OUTCOME_SUCCESS
from wt_media_agent.executors.protocol import ExecutorFactory
from wt_media_agent.runner.config import TaskRunnerConfig
from wt_media_agent.runner.registry import UnwiredTransfer
from wt_media_agent.runner.transfer import TRANSFER_STATUS, TransferRunner
from wt_media_agent.runtime.constants import TASK_TYPE_MATERIAL_DOWNLOAD
from wt_media_agent.storage import CheckpointStore, TaskCheckpoint
from wt_media_agent.storage.migration import apply_migrations
from wt_media_agent.storage.transfer_resume import TransferResume

TASK_ID = "transfer-1"
AGENT_ID = "agent-1"
CREDENTIAL = "node-credential-SENTINEL"
FILE_NAME = "春日-42.mp4"
LOGGER = "wt_media_agent.runner.transfer"

#: Every ending the executor can report that is not a confirmed success. The row
#: is what the next attempt continues from, and each of these can be handed the
#: same task again: Cloud retries a failed transfer within its attempt budget, a
#: lost lease can be re-claimed, and a report that never landed leaves the file
#: here with Cloud still waiting for it.
SURVIVING_OUTCOMES = ("failed", "lease_lost", "rejected", "unreported")


def make_lease(**overrides: object) -> TransferLease:
    fields: dict[str, object] = {
        "task_id": TASK_ID,
        "asset_id": 42,
        "title": "春日",
        "total_bytes": 4096,
        "expected_sha256": "a" * 64,
        "lease_seconds": 60,
        "download_url": "https://cdn.invalid/material?signature=SENTINEL",
        "download_url_expires_at": "2026-09-27T10:00:00Z",
    }
    fields.update(overrides)
    return TransferLease(**fields)  # type: ignore[arg-type]


class FakeTransferCloud:
    """The transfer surface, recording what it was told.

    Both halves of the wire are here -- the claim the loop makes and the
    completion an executor makes -- because the pairing is what turns "nothing
    was reported" into an assertion: the same fake records a completion in the
    arm where an executor reports one.
    """

    def __init__(
        self, leases: "list[TransferLease] | None" = None, raises: Exception | None = None
    ) -> None:
        self.leases = list(leases or [])
        self.raises = raises
        self.claims: list[str] = []
        self.completions: list[tuple[str, dict]] = []

    def claim_transfer_task(self, credential: str):
        self.claims.append(credential)
        if self.raises is not None:
            raise self.raises
        return self.leases.pop(0) if self.leases else None

    def complete_transfer_task(self, credential: str, task_id: str, **fields: object):
        self.completions.append((task_id, fields))
        return TransferTerminal(
            task_id=task_id,
            status=str(fields.get("status")),
            completed_bytes=int(fields.get("completed_bytes") or 0),
            file_name=fields.get("file_name"),  # type: ignore[arg-type]
        )


class TransferRunnerTest(unittest.TestCase):
    """A migrated store, a fake Cloud, and a runner wired to both."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        apply_migrations(Path(self._tmp.name) / "agent.db")
        self.store = CheckpointStore(Path(self._tmp.name) / "agent.db")
        self.cloud = FakeTransferCloud()
        # What the runner's `credential` callable answers; `None` is an Agent that
        # Desktop has not bound yet, which is the state this process starts in.
        self.credential: str | None = CREDENTIAL
        self.seen: list[tuple[object, str, dict]] = []

    def executor(self, outcome: dict) -> ExecutorFactory:
        """A factory whose product records the task it was handed."""
        # Bound outside the product: inside `execute`, `self` is the fake
        # executor, and reaching for the test case's attributes from there would
        # be an `AttributeError` the loop's own `except` would swallow.
        seen = self.seen

        def factory(client, agent_id):
            class FakeExecutor:
                def execute(self, task):
                    seen.append((client, agent_id, dict(task)))
                    return outcome

            return FakeExecutor()  # type: ignore[return-value]

        return factory

    def runner(self, executors=None, cloud=None) -> TransferRunner:
        """A runner on the fake Cloud. `executors=None` wires the answering fake.

        `poll_interval` is 0 so that nothing here sleeps; `start()` is driven in
        exactly one arm, and the rest call `_poll_once` directly.
        """
        return TransferRunner(
            cloud or self.cloud,  # type: ignore[arg-type]
            self.store,
            TaskRunnerConfig(
                agent_id=AGENT_ID,
                base_url="http://127.0.0.1:8188",
                db_path=":memory:",
                poll_interval=0.0,
            ),
            credential=lambda: self.credential,
            executors={TASK_TYPE_MATERIAL_DOWNLOAD: self.executor({"status": OUTCOME_SUCCESS})}
            if executors is None
            else executors,
        )

    def plant_record(self, name: str = FILE_NAME, done: int = 4096) -> None:
        self.store.save_checkpoint(
            TaskCheckpoint(
                task_id=TASK_ID,
                task_type=TASK_TYPE_MATERIAL_DOWNLOAD,
                agent_id=AGENT_ID,
                checkpoint_status=TRANSFER_STATUS,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
                transfer_state_json=TransferResume(name, done).encode(),
            )
        )

    # ---- what the executor is handed ----

    def test_the_executor_gets_the_lease_and_the_record_this_agent_holds(self) -> None:
        lease = make_lease()
        self.cloud.leases = [lease]
        self.plant_record()

        self.runner()._poll_once()

        self.assertEqual(len(self.seen), 1)
        client, agent_id, task = self.seen[0]
        self.assertIs(client, self.cloud)
        self.assertEqual(agent_id, AGENT_ID)
        self.assertEqual(task, {"lease": lease, "resume": TransferResume(FILE_NAME, 4096)})
        self.assertEqual(self.cloud.claims, [CREDENTIAL])

    def test_a_task_with_no_record_is_handed_over_without_one(self) -> None:
        """The positive control for the pair above: `resume` is a fact, not a constant."""
        self.cloud.leases = [make_lease()]

        self.runner()._poll_once()

        self.assertIsNone(self.seen[0][2]["resume"])

    def test_nothing_is_dispatched_when_cloud_has_no_transfer_to_give(self) -> None:
        self.runner()._poll_once()

        self.assertEqual(self.seen, [])
        self.assertEqual(self.cloud.claims, [CREDENTIAL])

    # ---- what happens to the row afterwards ----

    def test_a_confirmed_transfer_has_its_row_removed(self) -> None:
        """Cloud took it, so the task cannot come back and there is nothing to continue."""
        self.cloud.leases = [make_lease()]
        self.plant_record()

        self.runner()._poll_once()

        self.assertIsNone(self.store.get_checkpoint(TASK_ID))

    def test_the_row_survives_every_ending_the_task_may_come_back_from(self) -> None:
        for outcome in SURVIVING_OUTCOMES:
            with self.subTest(outcome=outcome):
                self.cloud.leases = [make_lease()]
                self.plant_record()

                self.runner(
                    executors={TASK_TYPE_MATERIAL_DOWNLOAD: self.executor({"status": outcome})}
                )._poll_once()

                checkpoint = self.store.get_checkpoint(TASK_ID)
                self.assertIsNotNone(checkpoint, f"the row was dropped for {outcome}")
                # And it is still the record itself, not merely a row: the next
                # attempt continues this file rather than naming a second one.
                self.assertEqual(
                    TransferResume.decode(checkpoint.transfer_state_json),  # type: ignore[union-attr]
                    TransferResume(FILE_NAME, 4096),
                )

    def test_an_executor_that_raises_leaves_the_row_alone(self) -> None:
        """Nothing about a crash says the part on disk is not still resumable."""

        def factory(client, agent_id):
            class Broken:
                def execute(self, task):
                    raise RuntimeError("this process is broken")

            return Broken()  # type: ignore[return-value]

        self.cloud.leases = [make_lease()]
        self.plant_record()

        self.runner(executors={TASK_TYPE_MATERIAL_DOWNLOAD: factory})._poll_once()

        self.assertIsNotNone(self.store.get_checkpoint(TASK_ID))

    # ---- the record writer the executor is given ----

    def test_the_record_writer_creates_the_row_this_loop_owns(self) -> None:
        runner = self.runner()

        runner.record(TASK_ID, TransferResume(FILE_NAME, 4096))

        checkpoint = self.store.get_checkpoint(TASK_ID)
        self.assertIsNotNone(checkpoint)
        self.assertEqual(checkpoint.task_type, TASK_TYPE_MATERIAL_DOWNLOAD)  # type: ignore[union-attr]
        self.assertEqual(checkpoint.agent_id, AGENT_ID)  # type: ignore[union-attr]
        # The literal, not the constant: `assertEqual(x, TRANSFER_STATUS)` would
        # agree with any value the constant was given, and the value is what the
        # two readers below look for.
        self.assertEqual(checkpoint.checkpoint_status, "transferring")  # type: ignore[union-attr]
        self.assertEqual(checkpoint.checkpoint_status, TRANSFER_STATUS)  # type: ignore[union-attr]
        # And the fact that value exists for: a transfer row is not a task.
        # `get_incomplete_checkpoints` is what `TaskRunner`'s restart summary and
        # `/api/v1/status` read, and they select on the status this row must not
        # have -- a row that appeared there would report a download as the
        # Agent's current task, with a progress of 0 for its whole life.
        self.assertEqual(self.store.get_incomplete_checkpoints(), [])
        # The writer and the reader are one pair, and this is the pair.
        self.assertEqual(runner.resume_of(TASK_ID), TransferResume(FILE_NAME, 4096))

    def test_the_record_writer_updates_a_row_without_replacing_its_other_facts(self) -> None:
        """`save_checkpoint` is an `INSERT OR REPLACE`, so a rebuilt row loses them.

        `created_at` and `progress` are not this loop's to write, and a writer
        that constructed a fresh checkpoint on every report would reset both --
        making a row that has been running for an hour look like it started one
        report ago.
        """
        self.plant_record(done=1)
        persisted = self.store.get_checkpoint(TASK_ID)
        persisted.progress = 7  # type: ignore[union-attr]
        self.store.save_checkpoint(persisted)  # type: ignore[arg-type]

        runner = self.runner()
        runner.record(TASK_ID, TransferResume(FILE_NAME, 4096))

        checkpoint = self.store.get_checkpoint(TASK_ID)
        self.assertEqual(checkpoint.created_at, "2026-01-01T00:00:00Z")  # type: ignore[union-attr]
        self.assertEqual(checkpoint.progress, 7)  # type: ignore[union-attr]
        self.assertEqual(runner.resume_of(TASK_ID), TransferResume(FILE_NAME, 4096))

    # ---- refusals the loop has to survive ----

    def test_an_unbound_agent_claims_nothing(self) -> None:
        """No credential, no call: the whole identity of the request is missing.

        Not a refusal and not a redial -- Cloud would answer 401 to an Agent that
        had not been bound yet, and asking would state something false about this
        process. The loop keeps its schedule, because Desktop binds it *after*
        starting it.
        """
        self.credential = None
        self.cloud.leases = [make_lease()]

        self.runner()._poll_once()

        self.assertEqual(self.cloud.claims, [])
        self.assertEqual(self.seen, [])

    def test_a_refused_credential_stops_claiming_until_it_is_replaced(self) -> None:
        """A 401 under one credential, and the loop works again under the next.

        An earlier version of this loop drained itself here, on the grounds that
        nothing it did next could work. That was true of a credential fixed at
        assembly and is false of one Desktop replaces: a drained loop is dead
        until somebody restarts the Agent, which is a download that never works
        again for a reason no operator would guess at. `_running` is the fact
        `start()`'s `while` reads, and the condition is set first the way
        `start()` sets it -- an assertion that it is true would otherwise hold
        for a runner that had never started.
        """
        self.cloud.raises = SessionInvalidError("Cloud refused this node's credential (HTTP 401)")
        runner = self.runner()
        runner._running = True

        runner._poll_once()
        runner._poll_once()

        # Once, not once per poll interval: the same refused credential is not
        # worth asking about again, and a poll is a second by default.
        self.assertEqual(len(self.cloud.claims), 1)
        self.assertTrue(runner._running)

        # And a replaced credential is tried at the next poll, not at the next
        # process start.
        self.cloud.raises = None
        self.cloud.leases = [make_lease()]
        self.credential = "a-credential-Cloud-issued-later"

        runner._poll_once()

        self.assertEqual(
            self.cloud.claims, [CREDENTIAL, "a-credential-Cloud-issued-later"]
        )
        self.assertEqual(len(self.seen), 1)

    def test_a_claim_that_fails_for_another_reason_is_not_fatal(self) -> None:
        """A dead network is the ordinary case here; the loop keeps its schedule.

        The condition is set first, the way `start()` sets it: a failure that
        left `_running` alone and one that cleared it are the same reading on a
        runner that was never started, and only one of them is the answer.
        """
        self.cloud.raises = RuntimeError("connection refused")
        runner = self.runner()
        runner._running = True

        runner._poll_once()

        self.assertTrue(runner._running)
        self.assertEqual(self.seen, [])
        self.assertIsNone(self.store.get_checkpoint(TASK_ID))

    def test_claim_transport_warning_is_deduplicated_and_resets_after_recovery(self) -> None:
        self.cloud.raises = TransferUnavailableError("Cloud TLS certificate verification failed")
        runner = self.runner()
        with self.assertLogs(LOGGER, level=logging.WARNING) as logs:
            runner._poll_once()
            runner._poll_once()
            self.cloud.raises = None
            runner._poll_once()
            self.cloud.raises = TransferUnavailableError("Cloud TLS certificate verification failed")
            runner._poll_once()
        warnings = [record for record in logs.records if record.levelno == logging.WARNING]
        self.assertEqual(len(warnings), 2)
        self.assertTrue(all("TLS certificate verification failed" in r.message for r in warnings))

    def test_claim_warning_does_not_copy_server_detail(self) -> None:
        self.cloud.raises = TransferUnavailableError(
            "the transfer endpoint answered HTTP 503: https://cdn.test/x?signature=SECRET"
        )
        with self.assertLogs(LOGGER, level=logging.WARNING) as logs:
            self.runner()._poll_once()
        self.assertIn("HTTP 503", logs.output[0])
        self.assertNotIn("SECRET", logs.output[0])

    def test_a_transfer_type_with_no_executor_is_reported_in_the_log(self) -> None:
        """No entry at all and an unwired entry are the same fact to the operator."""
        self.cloud.leases = [make_lease()]

        with self.assertLogs(LOGGER, level=logging.DEBUG) as logs:
            self.runner(executors={})._poll_once()

        self.assertIn("no_executor", [getattr(r, "error_code", None) for r in logs.records])

    def test_a_download_nothing_is_wired_for_is_never_reported_to_cloud(self) -> None:
        """The refusal is local, and the lease is Cloud's to expire.

        A report would need a name from the frozen vocabulary, and every name in
        that file says something about a download that this state is not. The
        positive control comes first: an executor that reports its own ending
        does reach this fake, so an empty list below means the runner held it
        back rather than that nothing here can record a completion.
        """

        def reporting(client, agent_id):
            class Reporting:
                def execute(self, task):
                    client.complete_transfer_task(
                        CREDENTIAL, task["lease"].task_id, status="failed",
                        error_code="download_stalled",
                    )
                    return {"status": "failed"}

            return Reporting()  # type: ignore[return-value]

        self.cloud.leases = [make_lease()]
        self.runner(executors={TASK_TYPE_MATERIAL_DOWNLOAD: reporting})._poll_once()
        self.assertEqual([task_id for task_id, _ in self.cloud.completions], [TASK_ID])

        self.cloud.completions.clear()
        self.cloud.leases = [make_lease()]
        self.runner(executors={})._poll_once()
        self.assertEqual(self.cloud.completions, [])

        self.cloud.leases = [make_lease()]
        with self.assertLogs(LOGGER, level=logging.DEBUG) as logs:
            self.runner(executors={TASK_TYPE_MATERIAL_DOWNLOAD: UnwiredTransfer})._poll_once()
        self.assertEqual(self.cloud.completions, [])
        self.assertIn("transfer_not_wired", [getattr(r, "error_code", None) for r in logs.records])


if __name__ == "__main__":
    unittest.main()
