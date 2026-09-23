"""BitBrowser Local API adapter.

The only module in the Agent that talks to the BitBrowser Local API. Per
ADR-0016 nothing outside `clients/` may construct `BitBrowserClient`; it is
built once in bootstrap and injected inward.
"""

from wt_media_agent.clients.bitbrowser.client import (
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
    "BitBrowserClient",
    "BitBrowserError",
    "BitBrowserIdentityError",
    "BitBrowserResponseError",
    "BitProfile",
    "ProfileSnapshot",
    "Transport",
    "bitbrowser_from_config",
]
