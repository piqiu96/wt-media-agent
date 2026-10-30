"""The loop that runs leased file transfers.

A download is not a generic Cloud task. It arrives on its own surface and holds
a lease that must be renewed while bytes move, so it has its own loop and poll.

**When it is allowed to claim at all.** Every one of the four transfer calls is
identified by the Agent Token and by nothing else, and this loop is started
before Desktop has handed this process a token: Desktop logs in, exchanges the
session for an Agent Token, then passes it to the sidecar. So the token arrives
as a callable (`Credential`) rather than a value, an unbound process claims
nothing, and a token Cloud refuses is not retried until it is replaced -- see
`_claim`.

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
from typing import Callable, Optional

from wt_media_agent.clients.cloud import (
    AgentAuthError,
    CloudAgentClient,
    TransferLease,
    TransferUnavailableError,
)
from wt_media_agent.clients.cloud.transfer import (
    COMPOSITE_OUTPUT_ASSET_TYPE,
    MATERIAL_ASSET_TYPE,
)
from wt_media_agent.executors.material_download import MaterialDownloadExecutor, OUTCOME_SUCCESS, Credential
from wt_media_agent.runner.config import TransferRunnerConfig
from wt_media_agent.runtime.constants import TASK_TYPE_MATERIAL_DOWNLOAD
from wt_media_agent.storage.checkpoint_store import CheckpointStore, TaskCheckpoint
from wt_media_agent.storage.transfer_resume import TransferResume
from wt_media_agent.utils.time import utc_now_iso

logger = logging.getLogger(__name__)

ExecutorFactory = Callable[[CloudAgentClient, str], MaterialDownloadExecutor]


class TransferNotWired(RuntimeError):
    """A transfer reached an Agent started without a download executor."""


class UnwiredTransfer:
    """Fail closed when the process did not register a download executor."""

    def __init__(self, client: CloudAgentClient, agent_id: str) -> None:
        del client, agent_id

    def execute(self, task: Mapping[str, object]) -> Mapping[str, object]:
        raise TransferNotWired(
            "this Agent has no download executor: file transfers cannot run here"
        )


#: The `checkpoint_status` a transfer's row carries.
#:
#: Deliberately not `running`. `/api/v1/status` projects the first incomplete row
#: as the Agent's current task, and a download there would report a
#: `current_task_progress` of 0 for its whole life, because the progress column
#: is a task percentage and a transfer's figure is a byte count. And
#: `get_incomplete_checkpoints` selects `('claimed', 'running')` only, so this
#: value also keeps a transfer row out of the retired generic restart summary.
TRANSFER_STATUS = "transferring"

#: Which executor each leasable asset type runs on. Both download kinds share
#: the one download executor (CHG-20261013-085): its naming and validation are
#: already driven by the lease, so a second executor would be a copy with the
#: same behaviour. A type missing here is one this Agent takes no work for --
#: the claim's parse has the same rule, and this map is the loop's own answer
#: to a lease it was handed by some other road.
EXECUTORS_BY_ASSET_TYPE = {
    MATERIAL_ASSET_TYPE: TASK_TYPE_MATERIAL_DOWNLOAD,
    COMPOSITE_OUTPUT_ASSET_TYPE: TASK_TYPE_MATERIAL_DOWNLOAD,
}


class TransferRunner:
    """Claims transfer leases from Cloud and runs them, one at a time."""

    def __init__(
        self,
        client: CloudAgentClient,
        store: CheckpointStore,
        config: TransferRunnerConfig,
        *,
        token: Credential,
        executors: Mapping[str, ExecutorFactory] | None = None,
    ) -> None:
        self.client = client
        self.store = store
        # `TransferRunnerConfig` carries only the facts this loop needs: the
        # poll interval and the agent id written on a checkpoint row.
        self.config = config
        # The whole identity of the four transfer calls: how to ask for it, not
        # the token itself, because Desktop hands it over after this loop is
        # already running -- and replaces it whenever it re-logs in. Never
        # composed into a message here.
        self._token = token
        # Empty by default: a loop nobody wired up fails closed rather than
        # reaching Cloud through an executor it built behind the caller's back.
        self._executors: dict[str, ExecutorFactory] = dict(executors or {})
        # The token value Cloud last refused, if any. Kept so that a refusal
        # does not turn into a poll-per-second stream of the same 401, while a
        # *replaced* token is tried at the next poll rather than at the next
        # process start: Desktop logs in again and pushes a new one, and the
        # whole point of handing this loop a callable is that it notices.
        self._refused: Optional[str] = None
        self._last_claim_failure: Optional[str] = None
        self._running = False

    def register_executor(self, task_type: str, factory: ExecutorFactory) -> None:
        self._executors[task_type] = factory

    # ---- Lifecycle ----

    def start(self) -> None:
        """Start the loop. Blocks until stopped.

        There is no recovery pass and no offline queue. A transfer's durable
        state is the resume record its executor reads when Cloud next hands it
        the task, and a terminal report that could not be delivered is Cloud's
        lease expiry to act on -- there is nothing here to replay at start-up.
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
        executor = self._executors.get(EXECUTORS_BY_ASSET_TYPE.get(lease.asset_type))
        if executor is None:
            # Not reported to Cloud, and that is a decision rather than an
            # omission: a refusal needs a name from the frozen vocabulary, and
            # every name in it says something about a download that this state is
            # not. An Agent with no download executor never claims anything --
            # the loop is what claims -- so the lease here is Cloud's to expire.
            logger.error(
                "no executor for transfer asset type %s", lease.asset_type,
                extra={"task_id": lease.task_id, "error_code": "no_executor"},
            )
            return

        task = {"lease": lease, "resume": self.resume_of(lease.task_id)}
        try:
            result = executor(self.client, self.config.agent_id).execute(task)
        except AgentAuthError as exc:
            # A download reports under the same token the claim used, so this
            # says the token stopped being accepted mid-transfer: the lease
            # cannot be renewed and the ending cannot be reported. The row
            # stays, and so does the loop -- the next claim is what discovers the
            # refusal for itself and stops claiming until the token is replaced
            # (this branch cannot reliably say *which* value was refused, and
            # guessing would suppress a token that had just been replaced under
            # it).
            logger.error(
                "transfer %s is uncertain after agent token refusal: %s", lease.task_id, exc,
                extra={"task_id": lease.task_id, "error_code": "agent_token_refused_result_uncertain"},
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
        token = self._token()
        if not token:
            # An Agent without a token has nothing to claim with. The four
            # transfer calls are identified by the token alone -- `claim` carries
            # neither a path parameter nor a body -- so there is no anonymous
            # claim to fall back on, and asking would only produce the 401 this
            # process already knows it would get. Not an error and not a reason
            # to stop: Desktop logs in and pushes a token afterwards, and until
            # it does the loop just keeps its schedule.
            logger.debug("no agent token yet; nothing to claim")
            return None
        if token == self._refused:
            # Already refused, and nothing has changed since. Asking again would
            # be the same 401 once per poll interval.
            logger.debug("agent token still refused; not claiming")
            return None
        try:
            lease = self.client.claim_transfer_task(token)
            self._last_claim_failure = None
            return lease
        except AgentAuthError as exc:
            # Stop claiming under *this* token; do not stop the loop. The next
            # login hands over a working token, and a drained loop would sit dead
            # until somebody restarted the Agent. It is still an error, because
            # the ordinary cause is that the task runtime is not logged in as it
            # thinks.
            self._refused = token
            logger.error(
                "Cloud refused this agent token (HTTP 401); "
                "not claiming until it is replaced: %s",
                exc,
            )
            return None
        except Exception as exc:
            # Repeated polls under one outage are one incident. Keep the first
            # useful diagnosis visible without copying arbitrary exception text,
            # which could include a signed URL or credential.
            if isinstance(exc, TransferUnavailableError):
                detail = str(exc)
                if detail == "Cloud TLS certificate verification failed":
                    reason = detail
                elif detail.startswith("the transfer endpoint answered HTTP "):
                    status = detail.removeprefix("the transfer endpoint answered HTTP ")[:3]
                    reason = f"HTTP {status}" if status.isdigit() else "HTTP error"
                else:
                    reason = "Cloud transport unavailable"
            else:
                reason = type(exc).__name__
            if reason != self._last_claim_failure:
                logger.warning("transfer claim unavailable: %s", reason)
                self._last_claim_failure = reason
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
