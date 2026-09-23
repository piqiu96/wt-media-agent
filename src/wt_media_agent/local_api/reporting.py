"""Response assembly helpers for the local control plane.

These shape the local API's own responses (safe group projection, request
duration, account-check item breakdown). They are not external adapters and not
shared local capabilities, so per ADR-0016 they belong to `local_api/` rather
than `clients/` or `services/`.
"""

from __future__ import annotations

import time


def _build_account_check_items(result: dict[str, object], has_expected: bool) -> list[dict[str, object]]:
    """Build check items 5-8 for the account check result (1-4 are Cloud-side).

    5=平台登录状态 6=登录账号与台账一致 7=需验证码/安全验证 8=账号限制/封号.
    7/8 判定需真实受限账号样本对齐，当前标记为 na（不适用/待对齐）。
    """
    uid = str(result.get("platform_account_id") or "").strip()
    login_status = str(result.get("login_status") or "")
    platform_login = "pass" if uid else ("fail" if login_status == "not_logged_in" else "na")
    platform_login_msg = "已读取到平台账号" if uid else ("未登录" if login_status == "not_logged_in" else "未获取到平台身份")

    if login_status == "account_mismatch":
        account_match = "fail"
        account_match_msg = "当前窗口登录账号与台账不一致"
    elif has_expected and uid:
        account_match = "pass"
        account_match_msg = "登录账号与台账一致"
    elif not uid:
        account_match = "na"
        account_match_msg = "未获取到平台身份，无法校验"
    else:
        account_match = "pass"
        account_match_msg = "登录账号与台账一致"

    return [
        {"key": "platform_login", "label": "平台登录状态", "status": platform_login, "message": platform_login_msg},
        {"key": "account_match", "label": "登录账号与台账一致", "status": account_match, "message": account_match_msg},
        {"key": "verification_needed", "label": "需验证码/安全验证", "status": "na", "message": "需真实受限账号样本对齐"},
        {"key": "account_restricted", "label": "账号限制/封号", "status": "na", "message": "需真实受限账号样本对齐"},
    ]


def _safe_groups(groups: list[dict[str, object]]) -> list[dict[str, str]]:
    safe: list[dict[str, str]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_id = str(group.get("id") or group.get("groupId") or "").strip()
        group_name = str(group.get("name") or group.get("groupName") or "").strip()
        if group_id:
            safe.append({"id": group_id, "name": group_name or group_id})
    return safe


def _duration_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)
