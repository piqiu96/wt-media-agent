"""Operation-specific BitBrowser timeouts.

Pure arithmetic: no environment, no I/O, no client state. The configuration
layer decides *where* an override comes from (`runtime/config.py`, which is the
only module allowed to read the environment); this module decides what it means.

Extracted from `client.py`, which used to read the two environment variables
itself (CHG-056 T-03).
"""

from __future__ import annotations

#: Ceiling for a single Browser open/close/update. Those calls block until
#: BitBrowser finishes launching a real browser, which is far slower than any
#: other call on this API -- hence a floor well above the client's own timeout.
DEFAULT_OPERATION_TIMEOUT = 30.0


def effective_timeout(configured: float, override: float | None, fallback: float) -> float:
    """Return the timeout for one operation.

    Args:
        configured: the client's own timeout, used as a floor.
        override: configured override, or None when unset/unparseable.
        fallback: the operation's default when no override is configured.

    The result is never below `configured`. This is the rule the pre-T-03 code
    implemented as `max(default_timeout, timeout)` and it is load-bearing: an
    override can only ever *raise* a timeout, so a configuration mistake can
    stall an operation but cannot silently cut a browser launch short.
    """
    return max(configured, fallback if override is None else override)
