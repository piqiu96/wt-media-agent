"""The loop that runs file transfers, beside `TaskRunner` rather than inside it.

A download is not a task this Agent claims from `/tasks/claim`. It arrives on
its own surface, its identity is a node credential rather than an agent session,
and what it holds is a lease that must be renewed while bytes move. `TaskRunner`
has none of those things and is tested without them, so a download runs in its
own loop with its own poll. The two share the registry's task-type to executor
inventory and nothing else (the user's ruling of 2026-09-27).

**Where the durable state lives.** A transfer's record is a `TransferResume` --
a name and a byte count -- and it goes on the task's row in the checkpoint table,
in the `transfer_state_json` column the checkpoint store already owns. The row
exists so a re-issued task continues the same file instead of downloading a
second copy beside it, so it is written when the executor first tells Cloud a
figure, and it is removed only when Cloud confirms the transfer finished. Every
other ending keeps it, including one where the file is complete and Cloud could
not be told: the next attempt reads the record, finds the bytes already there,
and completes the transfer rather than fetching it again.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Optional

from wt_media_agent.clients.cloud import (
    CloudAgentClient,
    SessionInvalidError,
    TransferLease,
)
from wt_media_agent.executors.material_download import OUTCOME_SUCCESS
from wt_media_agent.executors.protocol import ExecutorFactory
from wt_media_agent.runner.config import TaskRunnerConfig
from wt_media_agent.runner.registry import TransferNotWired
from wt_media_agent.runtime.constants import TASK_TYPE_MATERIAL_DOWNLOAD
from wt_media_agent.storage.checkpoint_store import CheckpointStore, TaskCheckpoint
from wt_media_agent.storage.transfer_resume import TransferResume
from wt_media_agent.utils.time import utc_now_iso

logger = logging.getLogger(__name__)

#: The `checkpoint_status` a transfer's row carries.
#:
#: Deliberately not `running`. `/api/v1/status` projects the first incomplete row
#: as the Agent's current task, and a download there would report a
#: `current_task_progress` of 0 for its whole life, because the progress column
#: is a task percentage and a transfer's figure is a byte count. And
#: `get_incomplete_checkpoints` selects `('claimed', 'running')` only, so this
#: value also keeps a transfer row out of `TaskRunner`'s restart summary, which
#: would otherwise promise to resume a row that loop never touches.
TRANSFER_STATUS = "transferring"


class TransferRunner:
    """Claims transfer leases from Cloud and runs them, one at a time."""

    def __init__(
        self,
        client: CloudAgentClient,
        store: CheckpointStore,
        config: TaskRunnerConfig,
        *,
        credential: str,
        executors: Mapping[str, ExecutorFactory] | None = None,
    ) -> None:
        self.client = client
        self.store = store
        # `TaskRunnerConfig` and not a second dataclass: this loop needs the poll
        # interval and the agent id that goes on a checkpoint row, and both are
        # the same facts about the same process. `lease_seconds` is unused here
        # -- the lease's own length comes down with it.
        self.config = config
        # The whole identity of the four transfer calls and of the executor's
        # reports: supplied by whoever obtained it, and never composed into a
        # message here.
        self._credential = credential
        # Empty by default, on purpose and for `TaskRunner`'s reason: a loop
        # nobody wired up must fail closed rather than reach Cloud through a
        # registry it built behind the caller's back.
        self._executors: dict[str, ExecutorFactory] = dict(executors or {})
        self._running = False

    def register_executor(self, task_type: str, factory: ExecutorFactory) -> None:
        self._executors[task_type] = factory

    # ---- Lifecycle ----

    def start(self) -> None:
        """Start the loop. Blocks until stopped.

        There is no recovery pass and no offline queue to flush, unlike
        `TaskRunner`. A transfer's durable state is the resume record its
        executor reads when Cloud next hands it the task, and a terminal report
        that could not be delivered is Cloud's lease expiry to act on -- there is
        nothing here to replay at start-up.
        """
        self._running = True
        while self._running:
            try:
                self._poll_once()
            except Exception as exc:
                logger.error("transfer runner poll error: %s", exc)
            time.sleep(self.config.poll_interval)

    def stop(self) -> None:
        self._running = False

    # ---- Poll Loop ----

    def _poll_once(self) -> None:
        lease = self._claim()
        if lease is None:
            return
        executor = self._executors.get(TASK_TYPE_MATERIAL_DOWNLOAD)
        if executor is None:
            # Not reported to Cloud, and that is a decision rather than an
            # omission: a refusal needs a name from the frozen vocabulary, and
            # every name in it says something about a download that this state is
            # not. An Agent with no download executor never claims anything --
            # the loop is what claims -- so the lease here is Cloud's to expire.
            logger.error(
                "no executor for transfer type %s", TASK_TYPE_MATERIAL_DOWNLOAD,
                extra={"task_id": lease.task_id, "error_code": "no_executor"},
            )
            return

        task = {"lease": lease, "resume": self.resume_of(lease.task_id)}
        try:
            result = executor(self.client, self.config.agent_id).execute(task)
        except SessionInvalidError as exc:
            self._running = False
            logger.error(
                "transfer %s is uncertain after session invalidation: %s", lease.task_id, exc,
                extra={"task_id": lease.task_id, "error_code": "session_invalidated_result_uncertain"},
            )
            return
        except TransferNotWired as exc:
            logger.error(
                "transfer %s cannot run: %s", lease.task_id, exc,
                extra={"task_id": lease.task_id, "error_code": "transfer_not_wired"},
            )
            return
        except Exception as exc:
            # An executor that reports its own faults should not raise, so this
            # is a fault in this process. The row stays: nothing here says the
            # file is not still resumable.
            logger.error(
                "transfer %s failed: %s", lease.task_id, exc,
                extra={"task_id": lease.task_id, "error_code": "executor_error"},
            )
            return

        if result.get("status") == OUTCOME_SUCCESS:
            self.store.remove_checkpoint(lease.task_id)

    def _claim(self) -> Optional[TransferLease]:
        try:
            return self.client.claim_transfer_task(self._credential)
        except SessionInvalidError as exc:
            self._running = False
            logger.error("node credential refused; draining transfer runner: %s", exc)
            return None
        except Exception as exc:
            logger.debug("transfer claim failed (may be normal): %s", exc)
            return None

    # ---- The row this loop owns ----

    def resume_of(self, task_id: str) -> Optional[TransferResume]:
        """What the last attempt at this task left to continue from, if anything.

        Read by task id with no status filter: the record is the row's content,
        and `TransferResume.decode` owns what counts as readable -- an absent or
        unreadable one answers "start from the beginning", which is always safe
        because the size and the digest are checked before anything is renamed.
        """
        checkpoint = self.store.get_checkpoint(task_id)
        if checkpoint is None:
            return None
        return TransferResume.decode(checkpoint.transfer_state_json)

    def record(self, task_id: str, resume: TransferResume) -> None:
        """Write a resume record onto this task's row, creating the row if needed.

        This is the `Recorder` the executor is handed, and the reason it takes a
        task id: one factory is built for the whole process, so the call that has
        a task in hand is the only thing that can say which row the record goes on.

        A row that already exists is updated and not replaced -- `save_checkpoint`
        is an `INSERT OR REPLACE`, so a checkpoint built from scratch here would
        drop the `created_at` the first attempt wrote.
        """
        now = utc_now_iso()
        checkpoint = self.store.get_checkpoint(task_id) or TaskCheckpoint(
            task_id=task_id,
            task_type=TASK_TYPE_MATERIAL_DOWNLOAD,
            agent_id=self.config.agent_id,
            checkpoint_status=TRANSFER_STATUS,
            created_at=now,
            updated_at=now,
        )
        checkpoint.transfer_state_json = resume.encode()
        checkpoint.updated_at = now
        self.store.save_checkpoint(checkpoint)
