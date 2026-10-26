"""账号检测执行器：打开 Profile，读取真实登录态并上报。"""

from __future__ import annotations

from collections.abc import Mapping

from wt_media_agent.clients import platform_identity
from wt_media_agent.clients.baijiahao import identity as baijiahao_identity
from wt_media_agent.clients.bilibili import identity as bilibili_identity
from wt_media_agent.clients.bitbrowser import BitBrowserClient
from wt_media_agent.clients.platform_urls import login_url
from wt_media_agent.executors.protocol import TaskReporter
from wt_media_agent.services.browser import cookies as browser_cookies


class AccountCheckExecutor:
    """打开 BitBrowser Profile，识别平台账号并给出可投影的登录态。"""

    def __init__(
        self, reporter: TaskReporter, agent_id: str, bitbrowser: BitBrowserClient
    ) -> None:
        self.reporter = reporter
        self.agent_id = agent_id
        self.bitbrowser = bitbrowser

    def execute(self, task: Mapping[str, object]) -> Mapping[str, object]:
        task_id = str(task.get("task_id", ""))
        payload = task.get("payload") if isinstance(task.get("payload"), Mapping) else task
        profile_id = str(
            payload.get("profile_id", "") or payload.get("browser_profile_id", "")
        ).strip()
        platform = str(payload.get("platform", "")).strip().lower()
        account_id = str(payload.get("account_id", "")).strip()
        expected_platform_account_id = str(
            payload.get("platform_account_id", "")
            or payload.get("expected_platform_account_id", "")
        ).strip()

        if not task_id:
            raise ValueError("account_check_task requires task_id")
        if not profile_id:
            raise ValueError("account_check_task requires profile_id")
        # 未知平台是调用方错误，必须在打开 Profile 之前失败。
        navigate_url = login_url(platform)

        self.reporter.report_task(task_id, self.agent_id, "running", 10, "正在打开 Profile")
        devtools = self.bitbrowser.open_profile_with_devtools(profile_id)
        identity: dict[str, object] = {}
        cookies: list[dict[str, object]] = []
        try:
            self.reporter.report_task(
                task_id, self.agent_id, "running", 30, f"正在访问 {platform}"
            )
            self.bitbrowser.open_url(profile_id, navigate_url)

            self.reporter.report_task(
                task_id, self.agent_id, "running", 50, "正在读取 Cookie"
            )
            cookies = browser_cookies.read_account_cookies(
                self.bitbrowser, profile_id, devtools
            )
            if not cookies:
                identity = {
                    "platform_account_id": "",
                    "login_status": "not_logged_in",
                    "message": "Profile 无 Cookie，未登录",
                }
            else:
                self.reporter.report_task(
                    task_id, self.agent_id, "running", 70, f"正在识别 {platform} 账号"
                )
                identity = _identify(platform, cookies, devtools)
        finally:
            self.reporter.report_task(
                task_id, self.agent_id, "running", 90, "正在关闭 Profile"
            )
            try:
                self.bitbrowser.close_profile(profile_id)
            except Exception:  # noqa: BLE001 - 关闭失败不覆盖检测结果
                pass

        check_result = str(identity.get("login_status") or "environment_error")
        message = str(identity.get("message") or "")
        platform_account_id = str(identity.get("platform_account_id") or "").strip()
        if (
            expected_platform_account_id
            and platform_account_id
            and platform_account_id != expected_platform_account_id
        ):
            check_result = "account_mismatch"
            message = "当前窗口登录账号与媒体账号台账不一致"

        result = {
            "account_id": account_id,
            "profile_id": profile_id,
            "platform": platform,
            "platform_account_id": platform_account_id,
            "platform_account_name": str(identity.get("name") or ""),
            "avatar_url": str(identity.get("avatar_url") or ""),
            # Cloud 的 projectResult 只认 logged_in / not_logged_in，
            # 其余值一律投影为 environment_error。
            "login_status": _cloud_login_status(check_result),
            "check_result": check_result,
            "cookie_count": len(cookies),
        }
        return self.reporter.report_task(
            task_id, self.agent_id, "succeeded", 100, message, result
        )


def _identify(
    platform: str, cookies: list[dict[str, object]], devtools: str
) -> dict[str, object]:
    if platform == "baijiahao":
        return baijiahao_identity.identify_baijiahao(cookies)
    if platform == "bilibili":
        nav_data = None
        if devtools:
            try:
                nav_data = bilibili_identity.fetch_nav_via_cdp(devtools)
            except Exception:  # noqa: BLE001 - 昵称/头像缺失不影响登录判定
                nav_data = None
        return bilibili_identity.identify_bilibili(cookies, nav_data)
    return platform_identity.identify_platform_account(platform, cookies)


def _cloud_login_status(check_result: str) -> str:
    """把本地检测结论收敛到 Cloud 的三种登录态。"""
    if check_result == "normal":
        return "logged_in"
    if check_result in {"not_logged_in", "expired", "account_mismatch"}:
        return "not_logged_in"
    return "environment_error"
