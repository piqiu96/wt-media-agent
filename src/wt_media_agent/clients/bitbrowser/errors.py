"""BitBrowser adapter error types."""

from __future__ import annotations


class BitBrowserError(RuntimeError):
    """Base error for BitBrowser adapter failures."""


class BitBrowserResponseError(BitBrowserError):
    """The Local API was unavailable or returned an invalid response."""


class BitBrowserRejectedError(BitBrowserResponseError):
    """BitBrowser explicitly returned success=false."""


class BitBrowserUnavailableError(BitBrowserResponseError):
    """The BitBrowser Local API could not be reached."""


class BitBrowserIdentityError(BitBrowserError):
    """Profile ownership could not be verified safely."""
