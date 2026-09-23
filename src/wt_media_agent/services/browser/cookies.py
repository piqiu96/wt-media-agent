"""Cookie extraction for account identity checks.

Live cookies come from the running browser over CDP when a DevTools endpoint is
available; otherwise the profile's saved cookies are used. Cookie values are
sensitive and must never be logged.
"""

from __future__ import annotations

from wt_media_agent.services.browser import cdp


def read_account_cookies(bitbrowser, profile_id: str, devtools: str) -> list[dict[str, object]]:
    """Read the profile's cookies: live via CDP when a DevTools endpoint is available,
    otherwise fall back to /browser/detail saved cookies."""
    if devtools:
        try:
            return cdp.read_live_cookies(devtools)
        except Exception:  # noqa: BLE001 - fall back to saved cookies
            pass
    return bitbrowser.read_cookies(profile_id)
