"""BitBrowser Local API adapter.

The only package in the Agent that talks to the BitBrowser Local API. The
client is constructed exactly once per process, in `bootstrap/app.py` (through
`bitbrowser_from_config`, which lives here) and injected inward -- per ADR-0016
§2 a single assembly site, not "nobody outside `clients/`", since `clients/`
itself is where the factory belongs. `tests/test_dependency_boundaries.py` R5
holds the line.
"""

from wt_media_agent.clients.bitbrowser.client import (
    PROXY_PROBE_URL,
    BitBrowserClient,
    BitProfile,
    ProfileSnapshot,
    Transport,
)
from wt_media_agent.clients.bitbrowser.errors import (
    BitBrowserError,
    BitBrowserIdentityError,
    BitBrowserResponseError,
)
from wt_media_agent.clients.bitbrowser.factory import bitbrowser_from_config

__all__ = [
    "PROXY_PROBE_URL",
    "BitBrowserClient",
    "BitBrowserError",
    "BitBrowserIdentityError",
    "BitBrowserResponseError",
    "BitProfile",
    "ProfileSnapshot",
    "Transport",
    "bitbrowser_from_config",
]
