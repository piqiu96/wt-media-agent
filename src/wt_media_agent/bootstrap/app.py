"""Process assembly: the one place that constructs the Agent's components.

`bootstrap` is the only production initialisation entry (ADR-0016 §2). There is
no registry, no dependency-injection container and no service locator here --
what follows is an explicit, ordered sequence of statements, and every component
below is handed what it needs rather than reaching for it:

1. configuration (the only reader of the environment and of `config/`)
2. runtime directories
3. logging
4. the BitBrowser client -- the only construction site in `src/`
5. the Cloud client
6. storage: schema, then the checkpoint store and the save-directory store
7. the Cloud identity, empty until Desktop binds this process
8. the configuration both loops share
9. the transfer loop, so the download factory below can close over its `record`
10. executor factories, bound to the client from step 4
11. the download entry, into that loop out of the registry built in step 10
12. the task runner, given those factories instead of looking a registry up
13. the observable state the local API reports from

Nothing below this module reads the environment or builds a client: they all
receive a `Components`, which is what makes "the executor used the configured
client" an assertable property instead of a convention.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from wt_media_agent.clients.bitbrowser import BitBrowserClient, bitbrowser_from_config
from wt_media_agent.clients.cloud import CloudAgentClient
from wt_media_agent.executors.material_download import MaterialDownloadExecutor
from wt_media_agent.local_api.state import LocalAgentState
from wt_media_agent.runner.config import TaskRunnerConfig
from wt_media_agent.runner.registry import default_executor_factories
from wt_media_agent.runner.runner import TaskRunner
from wt_media_agent.runner.transfer import TransferRunner
from wt_media_agent.runtime.config import AgentConfig, load_config
from wt_media_agent.runtime.constants import (
    DEFAULT_LEASE_SECONDS,
    MAX_RETRIES,
    TASK_TYPE_MATERIAL_DOWNLOAD,
)
from wt_media_agent.runtime.logging import configure_from
from wt_media_agent.runtime.node_credential import NodeCredential
from wt_media_agent.storage.checkpoint_store import CheckpointStore
from wt_media_agent.storage.migration import DEFAULT_DB_NAME, apply_migrations
from wt_media_agent.storage.save_directory import SaveDirectoryStore


@dataclass(frozen=True)
class Components:
    """Everything a mode surface needs, already wired to each other."""

    config: AgentConfig
    state: LocalAgentState
    bitbrowser: BitBrowserClient
    cloud: CloudAgentClient
    store: CheckpointStore
    save_directories: SaveDirectoryStore
    node_credential: NodeCredential
    runner: TaskRunner
    transfers: TransferRunner


def start_task_loops(components: Components) -> list[threading.Thread]:
    """Start both polling loops, each on a thread of its own.

    Here rather than in each mode module because the answer is the same in all
    three, and the one thing that is easy to get wrong is easy to get wrong
    three times: both loops block until `stop()`, so one thread cannot run both,
    and a mode that started only `TaskRunner` would leave downloads running
    nowhere and saying nothing about it (which is what `bootstrap/local.py` did
    until this was written down once).

    Daemon threads, and nothing is joined: an Agent whose loops are still
    sleeping is not a reason to keep a process alive, and a transfer cut off
    part-way is exactly what the lease and the resume record are for.

    Returned so a caller can say what it started (`tests/test_bootstrap_loops.py`
    asserts on the names and the daemon flag). Callers that do not care ignore
    the value, as all three mode modules do.
    """
    loops = [
        threading.Thread(target=components.runner.start, name="agent-runner", daemon=True),
        threading.Thread(target=components.transfers.start, name="agent-transfers", daemon=True),
    ]
    for loop in loops:
        loop.start()
    return loops


def database_path(config: AgentConfig) -> Path:
    """The Agent's SQLite file, under the configured data directory."""
    return config.paths.data_dir / DEFAULT_DB_NAME


def build_components(config: AgentConfig | None = None) -> Components:
    """Assemble the Agent in the documented order.

    `config` is a parameter so a test can assemble against a resolved
    configuration without touching the environment or the checkout's `config/`.
    """
    # 1. Configuration. `load_config` reads `config/agent.toml` and the
    #    environment through one code path; nothing else in `src/` may read
    #    either (the AST test in T-05 enforces this).
    config = load_config() if config is None else config

    # 2. Runtime directories. Creation failure is not fatal -- `ensure()` never
    #    raises, so a sandboxed filesystem yields an Agent that starts and says
    #    why it cannot write.
    config.paths.ensure()

    # 3. Logging, so anything after this point is visible where the operator
    #    configured it to be.
    configure_from(config)

    db_path = database_path(config)

    # 4. The BitBrowser client. Loopback HTTP with the configured timeouts.
    bitbrowser = bitbrowser_from_config(config)

    # 5. The Cloud client, with the configured request timeout. It used to
    #    hard-code `timeout=10`.
    cloud = CloudAgentClient(
        config.cloud_base_url, timeout=config.cloud_timeout_seconds
    )

    # 6. Storage. The schema goes in before the store is used: a runner writing
    #    checkpoints into a database with no tables would fail on its first
    #    claim, and `apply_migrations` is idempotent.
    apply_migrations(db_path)
    store = CheckpointStore(db_path)
    # Same file, same schema: the operator's save-directory choice lives in
    # `agent_metadata`, a table migration 0001 created for exactly this and that
    # nothing wrote to until now.
    save_directories = SaveDirectoryStore(db_path)

    # 7. The Cloud identity this process works under. Cloud issues the node
    #    credential and Desktop hands it here on the bind call, so this starts
    #    empty and is filled in while the loops below are already running.
    node_credential = NodeCredential()

    # 8. Both loops' shared configuration. One value rather than two because
    #    every field in it is a fact about this process, not about a loop:
    #    `agent_id` goes on a checkpoint row either way, and a db path that
    #    differed between them would be two Agents sharing one directory.
    loop_config = TaskRunnerConfig(
        agent_id=config.agent_id,
        base_url=config.cloud_base_url,
        db_path=str(db_path),
        lease_seconds=DEFAULT_LEASE_SECONDS,
        max_retries=MAX_RETRIES,
    )

    # 9. The transfer loop, before the registry because the download factory
    #    closes over `record` -- the only thing that knows how to write a resume
    #    record. It is handed its executors below, once the registry exists.
    transfers = TransferRunner(
        cloud, store, loop_config, credential=node_credential.get
    )

    # 10. Executor factories. `bitbrowser` from step 4 is bound by closure, so
    #     every browser-driving executor shares this one instance and none of
    #     them can build its own.
    #
    #     The download executor's other three facts arrive the same way: the
    #     credential as a callable (Cloud replaces it, and the executor reports
    #     under whichever is current), the save directory as a callable (the
    #     operator may change it mid-download), and the record writer as the
    #     transfer loop's own `record`.
    def download_executor(client: CloudAgentClient, agent_id: str) -> MaterialDownloadExecutor:
        return MaterialDownloadExecutor(
            client,
            agent_id,
            credential=node_credential.get,
            save_directory=save_directories.get,
            record=transfers.record,
        )

    executors = default_executor_factories(bitbrowser, transfer=download_executor)

    # 11. The download entry reaches the transfer loop *through the registry*,
    #     which is what makes "one task-type to executor inventory, two loops"
    #     true rather than aspirational: a task type the registry answers for is
    #     dispatchable by either loop, and one it does not is dispatchable by
    #     neither.
    transfers.register_executor(
        TASK_TYPE_MATERIAL_DOWNLOAD, executors[TASK_TYPE_MATERIAL_DOWNLOAD]
    )

    # 12. The runner. It receives its registry as an argument; it has no default,
    #     so a runner that was not wired here would fail closed on `no_executor`.
    runner = TaskRunner(cloud, store, loop_config, executors=executors)

    # 13. Observable state for the local API surface.
    state = LocalAgentState(agent_id=config.agent_id)

    return Components(
        config=config,
        state=state,
        bitbrowser=bitbrowser,
        cloud=cloud,
        store=store,
        save_directories=save_directories,
        node_credential=node_credential,
        runner=runner,
        transfers=transfers,
    )
