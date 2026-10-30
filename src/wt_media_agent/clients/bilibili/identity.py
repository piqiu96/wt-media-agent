"""Bilibili account identity from a live in-page platform response.

Note the `clients -> services` edge: `fetch_nav_via_cdp` drives the browser over
CDP, which lives in `services/browser/cdp.py`. ADR-0016 §1 names the descent
`... -> {clients, services, storage}` without ordering the siblings, and §3's
explicit forbid list covers only `clients -> executors`, `services -> executors`
and `utils -> business layers`, so a sibling edge is permitted here. It is
recorded explicitly in the T-05 AST whitelist.
"""

from __future__ import annotations

from wt_media_agent.services.browser import cdp

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"


def fetch_nav_via_cdp(devtools: str) -> dict[str, object] | None:
    """Fetch Bilibili nav (mid/uname/face) from inside the page to bypass risk control."""
    try:
        nav = cdp.eval_fetch_json(devtools, NAV_URL)
        if nav.get("status") == 200:
            return nav.get("json") or {}
    except Exception:  # noqa: BLE001 - best-effort, never block identification
        pass
    return None


def identify_bilibili(cookies: list[dict[str, object]], nav_data: dict[str, object] | None = None) -> dict[str, object]:
    """Only a current platform response establishes login and account identity."""
    empty = {"platform_account_id": "", "name": "", "avatar_url": ""}
    nav = nav_data if isinstance(nav_data, dict) else None
    if nav is None:
        return {**empty, "login_status": "environment_error", "message": "B站实时身份接口未取得有效结果，请重试检查"}
    if nav.get("code") == -101:
        return {**empty, "login_status": "not_logged_in", "message": "B站当前未登录"}
    if nav.get("code") != 0:
        return {**empty, "login_status": "environment_error", "message": "B站实时身份接口暂不可用，请重试检查"}
    data = nav.get("data") or {}
    if not data.get("isLogin"):
        return {**empty, "login_status": "not_logged_in", "message": "B站当前未登录"}
    uid = str(data.get("mid") or "").strip()
    if not uid:
        return {**empty, "login_status": "environment_error", "message": "B站实时身份缺少UID，请重试检查"}
    return {"platform_account_id": uid, "name": str(data.get("uname") or "").strip(),
            "avatar_url": str(data.get("face") or "").strip(),
            "login_status": "normal", "message": "已同步B站当前登录账号"}
