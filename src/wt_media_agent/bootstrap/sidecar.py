"""Sidecar mode: the Local Agent API that Desktop supervises.

Desktop passes the loopback host, the port, the runtime token and the data
directory as environment variables, and `runtime/config.py` is what reads them
-- this module reads none of them itself. The token therefore never appears in
a command line, where `ps` would show it to every user on the machine.
"""

from __future__ import annotations

import threading

from wt_media_agent.bootstrap.app import build_components
from wt_media_agent.local_api.server import serve


def run() -> int:
    components = build_components()
    config = components.config
    if config.run_runner:
        # A sidecar is not only a status surface: with the switch on it also
        # executes tasks. The runner blocks, so it goes on its own thread and
        # the API keeps serving health and status while a task is in flight.
        threading.Thread(
            target=components.runner.start, name="agent-runner", daemon=True
        ).start()
    serve(
        config.local_api_host,
        config.local_api_port,
        bitbrowser=components.bitbrowser,
        checkpoint_store=components.store,
        state=components.state,
        auth_token=config.runtime_token,
    )
    return 0
