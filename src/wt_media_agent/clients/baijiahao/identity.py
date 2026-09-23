"""Baijiahao/Baidu account identity via the public logininfo API.

Baijiahao UID is not derivable from cookies alone (BDUSS is encrypted), so the
Agent calls image.baidu.com/user/logininfo server-side with the BDUSS cookie to
read uid / nickname / portrait. The BDUSS value is sensitive and must never be
logged; it is only sent as a request header.
"""

from __future__ import annotations

import json
import time
from urllib import parse as urlparse
from urllib import request as urlrequest

BAIJIAHAO_HOST = "https://image.baidu.com"
LOGININFO_URL_TEMPLATE = (
    BAIJIAHAO_HOST + "/user/logininfo?time={time_ms}&src=pc&page=index"
)
AVATAR_BASE_URL = "https://himg.bdimg.com/sys/portraitn/item/"


def identify_baijiahao(cookies: list[dict[str, object]]) -> dict[str, object]:
    """Identify a Baijiahao/Baidu account via the public logininfo API."""
    cookie_by_name: dict[str, str] = {}
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name", "")).strip()
        value = str(cookie.get("value", "")).strip()
        if name and value:
            cookie_by_name[name] = value
    bduss = cookie_by_name.get("BDUSS", "").strip()
    if not bduss:
        return {
            "platform_account_id": "",
            "name": "",
            "avatar_url": "",
            "login_status": "not_logged_in",
            "message": "未读取到百度登录凭证(BDUSS)",
        }
    try:
        url = LOGININFO_URL_TEMPLATE.format(time_ms=int(time.time() * 1000))
        req = urlrequest.Request(
            url,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": BAIJIAHAO_HOST + "/",
                "Cookie": f"BDUSS={urlparse.quote(bduss)}",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
                ),
            },
        )
        body = urlrequest.urlopen(req, timeout=10).read().decode(errors="replace")
        payload = json.loads(body)
        status = payload.get("status") or {}
        if status.get("code") != 0:
            return {
                "platform_account_id": "",
                "name": "",
                "avatar_url": "",
                "login_status": "expired",
                "message": f"百度登录校验失败: {status.get('msg', '')}",
            }
        user = payload.get("data", {}).get("user") or {}
        uid = str(user.get("user_id", "") or "").strip()
        name = str(user.get("user_name", "") or "").strip()
        portrait = str(user.get("portrait", "") or "").strip()
        if not uid:
            return {
                "platform_account_id": "",
                "name": "",
                "avatar_url": "",
                "login_status": "not_logged_in",
                "message": "未读取到百度账号UID",
            }
        avatar_url = f"{AVATAR_BASE_URL}{portrait}" if portrait else ""
        return {
            "platform_account_id": uid,
            "name": name,
            "avatar_url": avatar_url,
            "login_status": "normal",
            "message": "已读取到百家号账号信息",
        }
    except Exception as exc:  # noqa: BLE001 - surface readable message, never leak cookie
        return {
            "platform_account_id": "",
            "name": "",
            "avatar_url": "",
            "login_status": "environment_error",
            "message": f"百家号信息接口调用失败: {exc}",
        }
