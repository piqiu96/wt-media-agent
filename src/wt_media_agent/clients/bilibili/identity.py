"""Bilibili account identity from cookies, with in-page nav enrichment.

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
    """Identify a Bilibili account from cookies.

    UID comes reliably from the DedeUserID cookie. Nickname/avatar come from the
    in-page nav fetch (nav_data) when available; Bilibili's newer API enforces
    bili_ticket / fingerprint risk control that rejects server-side requests
    (-101 / -799), so a failed nav never downgrades a valid DedeUserID login.
    """
    cookie_by_name: dict[str, str] = {}
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name", "")).strip()
        value = str(cookie.get("value", "")).strip()
        if name and value:
            cookie_by_name[name] = value
    uid = cookie_by_name.get("DedeUserID", "").strip()
    if not uid:
        return {
            "platform_account_id": "",
            "name": "",
            "avatar_url": "",
            "login_status": "not_logged_in",
            "message": "未读取到B站登录Cookie(DedeUserID)",
        }
    name = ""
    face = ""
    nav = nav_data if isinstance(nav_data, dict) else None
    if nav and nav.get("code") == 0:
        data = nav.get("data") or {}
        name = str(data.get("uname", "") or "").strip()
        face = str(data.get("face", "") or "").strip()
    return {
        "platform_account_id": uid,
        "name": name,
        "avatar_url": face,
        "login_status": "normal",
        "message": "已读取到B站账号UID" + ("" if name else "（昵称/头像需页面内验证）"),
    }
