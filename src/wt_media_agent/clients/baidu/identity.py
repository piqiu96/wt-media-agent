"""Baidu account identity via the public logininfo API.

The Baidu UID is not derivable from cookies alone (BDUSS is encrypted), so the
Agent calls image.baidu.com/user/logininfo server-side with the BDUSS cookie to
read uid / nickname / portrait. The BDUSS value is sensitive and must never be
logged; it is only sent as a request header.
"""

from __future__ import annotations

import json
import time
from urllib import parse as urlparse
from urllib import request as urlrequest

BAIDU_LOGIN_HOST = "https://image.baidu.com"
LOGININFO_URL_TEMPLATE = (
    BAIDU_LOGIN_HOST + "/user/logininfo?time={time_ms}&src=pc&page=index"
)
AVATAR_BASE_URL = "https://himg.bdimg.com/sys/portraitn/item/"


def logininfo_url() -> str:
    return LOGININFO_URL_TEMPLATE.format(time_ms=int(time.time() * 1000))


def is_logininfo_url(raw: str) -> bool:
    parsed = urlparse.urlsplit(raw)
    return (parsed.scheme == 'https' and parsed.netloc == 'image.baidu.com'
            and parsed.path == '/user/logininfo' and not parsed.fragment)


def parse_baidu_identity(payload: object) -> dict[str, object]:
    """Normalize both observed logininfo shapes, without accepting stale UIDs."""
    payload = payload if isinstance(payload, dict) else {}
    status = payload.get('status')
    if isinstance(status, dict) and status.get('code', 0) != 0:
        return {'platform_account_id': '', 'name': '', 'avatar_url': '',
                'login_status': 'expired', 'message': '百度登录校验失败'}
    data = payload.get('data')
    user = data.get('user') if isinstance(data, dict) else None
    user = user if isinstance(user, dict) else {}
    value = user.get('user_id')
    uid = str(value) if type(value) in (str, int) else ''
    if not (uid and uid.isascii() and uid.isdecimal() and uid[0] != '0'):
        return {'platform_account_id': '', 'name': '', 'avatar_url': '',
                'login_status': 'not_logged_in', 'message': '未读取到百度账号UID'}
    portrait = str(user.get('portrait') or '').strip()
    return {'platform_account_id': uid, 'name': str(user.get('user_name') or '').strip(),
            'avatar_url': AVATAR_BASE_URL + portrait if portrait else '',
            'login_status': 'normal', 'message': '已读取到百度账号信息'}


def read_identity(fetch) -> dict[str, object]:
    """Use a supplied Profile HTTP or browser transport for the same protocol."""
    response = fetch(logininfo_url())
    return parse_baidu_identity(response.get('json') if response.get('status') == 200 else None)


def identify_baidu(cookies: list[dict[str, object]]) -> dict[str, object]:
    """Identify a Baidu account via the public logininfo API."""
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
        url = logininfo_url()
        req = urlrequest.Request(
            url,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": BAIDU_LOGIN_HOST + "/",
                "Cookie": f"BDUSS={urlparse.quote(bduss)}",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
                ),
            },
        )
        body = urlrequest.urlopen(req, timeout=10).read().decode(errors="replace")
        return parse_baidu_identity(json.loads(body))
    except Exception as exc:  # noqa: BLE001 - surface readable message, never leak cookie
        return {
            "platform_account_id": "",
            "name": "",
            "avatar_url": "",
            "login_status": "environment_error",
            "message": f"百度账号信息接口调用失败: {exc}",
        }
