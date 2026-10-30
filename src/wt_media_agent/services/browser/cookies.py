"""Read-only, live Cookie extraction for account identity checks."""
from __future__ import annotations
from wt_media_agent.services.browser import cdp

class LiveCookieReadError(RuntimeError):
    pass

def read_account_cookies(bitbrowser, profile_id: str, devtools: str) -> list[dict[str, object]]:
    # Saved profile configuration cannot prove the current browser session.
    if not devtools:
        raise LiveCookieReadError("未取得浏览器实时连接，请重新打开窗口后检查")
    try:
        return cdp.read_live_cookies(devtools)
    except Exception as exc:
        raise LiveCookieReadError("浏览器实时会话读取失败，请重试检查") from exc
