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
7. the Agent Token, empty until Desktop hands one over
8. the transfer loop configuration
9. the transfer loop, so the download factory below can close over its `record`
10. the download executor, registered on that loop
11. the observable state the local API reports from

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
from wt_media_agent.runner.config import TransferRunnerConfig
from wt_media_agent.runner.transfer import TransferRunner
from wt_media_agent.runtime.agent_token import AgentToken
from wt_media_agent.runtime.config import AgentConfig, configure_frozen_ca_bundle, load_config
from wt_media_agent.runtime.constants import TASK_TYPE_MATERIAL_DOWNLOAD
from wt_media_agent.runtime.logging import configure_from
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
    agent_token: AgentToken
    transfers: TransferRunner


def start_transfer_loop(components: Components) -> list[threading.Thread]:
    """Start the file-transfer loop.

    The generic Cloud task loop was retired with `/cloud-agent/tasks/*`: browser
    operations are one-shot Local API calls, while file transfers keep their own
    leased surface and loop.
    """
    loop = threading.Thread(
        target=components.transfers.start,
        name="agent-transfers",
        daemon=True,
    )
    loop.start()
    return [loop]

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

    # PyInstaller's Python may have no system CA store on the installed Mac.
    # Configure the verified embedded trust store before constructing clients.
    configure_frozen_ca_bundle()

    db_path = database_path(config)

    # 4. The BitBrowser client. Loopback HTTP with the configured timeouts.
    bitbrowser = bitbrowser_from_config(config)

    # 5. The Cloud client for binding and file-transfer calls.
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

    # 7. The Cloud identity this process works under. Desktop exchanges its
    #    desktop session for an Agent Token and hands it here over the local
    #    API, so this starts empty and is filled in while the loops below are
    #    already running.
    agent_token = AgentToken()

    # 8. The transfer loop's configuration. Every field is a fact about this process:
    #    `agent_id` goes on a checkpoint row and the db path is shared state.
    loop_config = TransferRunnerConfig(
        agent_id=config.agent_id,
        base_url=config.cloud_base_url,
        db_path=str(db_path),
    )

    # 9. The transfer loop, before its executor because the download factory
    #    closes over `record` -- the only thing that knows how to write a resume
    #    record.
    transfers = TransferRunner(
        cloud, store, loop_config, token=agent_token.get
    )

    # 10. The file-transfer executor. The generic browser task runner is gone;
    #     one-shot browser work arrives through the Local API.
    def download_executor(client: CloudAgentClient, agent_id: str) -> MaterialDownloadExecutor:
        return MaterialDownloadExecutor(
            client,
            agent_id,
            token=agent_token.get,
            save_directory=save_directories.get,
            record=transfers.record,
        )

    transfers.register_executor(TASK_TYPE_MATERIAL_DOWNLOAD, download_executor)

    # 11. Observable state for the local API surface.
    state = LocalAgentState(agent_id=config.agent_id)

    return Components(
        config=config,
        state=state,
        bitbrowser=bitbrowser,
        cloud=cloud,
        store=store,
        save_directories=save_directories,
        agent_token=agent_token,
        transfers=transfers,
    )
