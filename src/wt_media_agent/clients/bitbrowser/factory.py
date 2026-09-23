"""The one place that decides how a `BitBrowserClient` is built.

ADR-0016 requires that `BitBrowserClient` be constructed inside `clients/` and
injected inward. Bootstrap owns *when* -- once, during assembly -- and this
module owns *how*: which configuration keys supply which parameter. Before
CHG-056 T-03 five call sites each read the environment for themselves; now there
is one construction recipe, and after T-04 there is one caller.

`transport` exists so tests can inject a fake; production leaves it None and the
client uses the real HTTP transport.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from wt_media_agent.clients.bitbrowser.client import BitBrowserClient, Transport

if TYPE_CHECKING:
    from wt_media_agent.runtime.config import AgentConfig


def bitbrowser_from_config(
    config: AgentConfig, *, transport: Transport | None = None
) -> BitBrowserClient:
    """Build a client from resolved configuration."""
    return BitBrowserClient(
        config.bitbrowser_api_url,
        transport=transport,
        timeout=config.bitbrowser_timeout_seconds,
        create_timeout_override=config.bitbrowser_create_timeout_seconds,
        mutation_timeout_override=config.bitbrowser_mutation_timeout_seconds,
    )
