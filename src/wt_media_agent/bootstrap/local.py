"""Local mode: the Agent runs as its own loopback service.

This is the `wt-media-local-agent` console script. It is the same surface a
packaged Desktop sidecar uses; the sidecar differs only in where its parameters
come from, not in what runs.
"""

from __future__ import annotations

from wt_media_agent.bootstrap.app import build_components
from wt_media_agent.local_api.server import serve


def run() -> int:
    components = build_components()
    config = components.config
    serve(
        config.local_api_host,
        config.local_api_port,
        bitbrowser=components.bitbrowser,
        checkpoint_store=components.store,
        state=components.state,
        auth_token=config.runtime_token,
        cloud_base_url=config.cloud_base_url,
    )
    return 0
