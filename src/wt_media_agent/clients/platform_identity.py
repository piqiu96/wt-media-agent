"""Generic cookie-based platform account identity (douyin and unknown platforms).

Platforms whose identity is readable straight from a cookie get handled here;
platforms needing a server-side API call or in-page enrichment live in their own
`clients/<platform>/` package.
"""

from __future__ import annotations


def identify_platform_account(platform: str, cookies: list[dict[str, object]]) -> dict[str, object]:
    """Extract only safe platform identity facts. Cookie values never leave this function except known public IDs."""
    cookie_by_name: dict[str, str] = {}
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name", "")).strip()
        value = str(cookie.get("value", "")).strip()
        if name and value:
            cookie_by_name[name] = value

    if not cookie_by_name:
        return {
            "platform_account_id": "",
            "name": "",
            "avatar_url": "",
            "login_status": "not_logged_in",
            "message": "当前窗口未读取到登录Cookie",
        }

    if platform == "bilibili":
        uid = cookie_by_name.get("DedeUserID", "").strip()
        if uid:
            return {
                "platform_account_id": uid,
                "name": "",
                "avatar_url": "",
                "login_status": "normal",
                "message": "已读取到哔哩哔哩账号UID",
            }

    return {
        "platform_account_id": "",
        "name": "",
        "avatar_url": "",
        "login_status": "environment_error",
        "message": "当前平台暂未读取到可确认的平台账号UID",
    }
