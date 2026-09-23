"""Platform destination URLs -- where the Agent drives a browser profile to.

`clients/` owns external-system protocol (ADR-0016 §2), and "which site do we
send this profile to" is that kind of fact: a platform's address is part of
speaking to it. These lived as a class attribute on
`executors/account_check.py`, which put `https://` literals in the one layer
that is supposed to reach the outside world only through clients and services
(ADR-0016 §3, enforced by `tests/test_dependency_boundaries.py` R9).

The douyin entry lives here rather than in a `clients/douyin/` package on
purpose: ADR-0016 §5 keeps the Agent from building platform directories it does
not implement, and all this table has to answer is which URL verifies a login.
"""

from __future__ import annotations

#: Login-verification destinations, keyed by the platform id tasks carry.
LOGIN_URLS: dict[str, str] = {
    "douyin": "https://www.douyin.com/",
    "bilibili": "https://www.bilibili.com/",
    "baijiahao": "https://baijiahao.baidu.com/",
}


def login_url(platform: str) -> str:
    """Return the login-verification URL for `platform`.

    Raises `ValueError` for an unknown platform: callers must surface that as a
    caller error rather than as a failed check.
    """
    try:
        return LOGIN_URLS[platform]
    except KeyError:
        raise ValueError(f"unsupported platform: {platform}") from None
