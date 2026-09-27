"""Local mode: the Agent runs as its own loopback service.

This is the `wt-media-local-agent` console script. It is the same surface a
packaged Desktop sidecar uses; the sidecar differs only in where its parameters
come from, not in what runs.
"""

from __future__ import annotations

from wt_media_agent.bootstrap.app import build_components, start_task_loops
from wt_media_agent.local_api.server import serve


def run() -> int:
    components = build_components()
    config = components.config
    if config.run_runner:
        # The docstring above says this is the same surface the sidecar runs, and
        # until CHG-061's assembly step it was not: `bootstrap/sidecar.py` started
        # the task loop and this did not, so the console script never polled
        # Cloud no matter what `run_runner` said. Same gate, same helper.
        start_task_loops(components)
    serve(
        config.local_api_host,
        config.local_api_port,
        bitbrowser=components.bitbrowser,
        checkpoint_store=components.store,
        save_directory_store=components.save_directories,
        node_credential=components.node_credential,
        state=components.state,
        auth_token=config.runtime_token,
        cloud_base_url=config.cloud_base_url,
    )
    return 0
