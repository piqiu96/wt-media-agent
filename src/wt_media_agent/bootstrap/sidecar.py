"""Sidecar mode: the Local Agent API that Desktop supervises.

Desktop passes the loopback host, the port, the runtime token and the data
directory as environment variables, and `runtime/config.py` is what reads them
-- this module reads none of them itself. The token therefore never appears in
a command line, where `ps` would show it to every user on the machine.
"""

from __future__ import annotations

from wt_media_agent.bootstrap.app import build_components, start_task_loops
from wt_media_agent.local_api.server import serve


def run() -> int:
    components = build_components()
    config = components.config
    if config.run_runner:
        # A sidecar is not only a status surface: with the switch on it also
        # executes tasks. Both loops block, so each gets its own thread and the
        # API keeps serving health and status while work is in flight.
        start_task_loops(components)
    serve(
        config.local_api_host,
        config.local_api_port,
        bitbrowser=components.bitbrowser,
        checkpoint_store=components.store,
        # Both of these were missing here until CHG-061's assembly step, and the
        # save-directory one was missing in silence: `serve` defaults the store
        # to `None`, the two save-directory routes answered "no directory chosen"
        # for a machine that had one, and the Desktop renders exactly that. The
        # 503 a missing store produces is what made it visible at all.
        save_directory_store=components.save_directories,
        node_credential=components.node_credential,
        state=components.state,
        auth_token=config.runtime_token,
        cloud_base_url=config.cloud_base_url,
    )
    return 0
