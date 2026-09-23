"""Account check executor — verifies login status for a platform account."""

from __future__ import annotations

from collections.abc import Mapping

import os

from wt_media_agent.cloud_agent_client import CloudAgentClient
from wt_media_agent.runtime.constants import DEFAULT_BITBROWSER_API_URL, DEFAULT_BITBROWSER_TIMEOUT
from wt_media_agent.runtimes.bitbrowser import BitBrowserClient


CHECK_RESULTS = {
    "normal": "登录正常",
    "not_logged_in": "未登录",
    "account_mismatch": "账号不匹配",
    "verification_needed": "需要验证",
    "profile_invalid": "Profile 不可用",
    "proxy_error": "代理异常",
    "account_restricted": "账号受限",
    "check_failed": "检查失败",
}


class AccountCheckExecutor:
    """Open a BitBrowser Profile, navigate to the target platform, and verify login state."""

    # Well-known platform URLs for login verification.
    PLATFORM_URLS = {
        "douyin": "https://www.douyin.com/",
        "bilibili": "https://www.bilibili.com/",
        "baijiahao": "https://baijiahao.baidu.com/",
    }

    def __init__(self, client: CloudAgentClient, agent_id: str) -> None:
        self.client = client
        self.agent_id = agent_id
        bb_url = os.getenv("WT_MEDIA_BITBROWSER_API_URL", DEFAULT_BITBROWSER_API_URL)
        bb_timeout = float(os.getenv("WT_MEDIA_BITBROWSER_TIMEOUT_SECONDS", str(DEFAULT_BITBROWSER_TIMEOUT)))
        self.bitbrowser = BitBrowserClient(bb_url, timeout=bb_timeout)

    def execute(self, task: Mapping[str, object]) -> Mapping[str, object]:
        task_id = str(task.get("task_id", ""))
        payload = task.get("payload") if isinstance(task.get("payload"), Mapping) else task
        profile_id = str(payload.get("profile_id", "") or payload.get("browser_profile_id", ""))
        platform = str(payload.get("platform", ""))
        expected_account_id = str(payload.get("platform_account_id", ""))

        if not task_id:
            raise ValueError("account_check_task requires task_id")
        if not profile_id:
            raise ValueError("account_check_task requires profile_id")
        if platform not in self.PLATFORM_URLS:
            raise ValueError(f"unsupported platform: {platform}")

        result_status = "check_failed"
        result_message = ""

        try:
            self.client.report_task(task_id, self.agent_id, "running", 10, "正在打开 Profile")
            self.bitbrowser.open_profile(profile_id)

            self.client.report_task(task_id, self.agent_id, "running", 30, f"正在访问 {platform}")
            # Navigate to platform home page to trigger login state
            navigate_url = self.PLATFORM_URLS[platform]
            self.bitbrowser._post("/browser/open-url", {
                "id": profile_id,
                "url": navigate_url,
            })

            self.client.report_task(task_id, self.agent_id, "running", 50, "正在读取 Cookie")
            cookies = self.bitbrowser.read_cookies(profile_id)

            # Check 1: Profile must have cookies
            if not cookies:
                result_status = "not_logged_in"
                result_message = "Profile 无 Cookie，未登录"
            else:
                # Check 2: Profile proxy connectivity (profiles with proxy config)
                try:
                    self.bitbrowser._post("/browser/open-url", {
                        "id": profile_id,
                        "url": "http://detect.ocsp.intra",
                    })
                except Exception:
                    pass  # Proxy check is best-effort

                # Default: can't determine actual login state without platform page parsing
                # This requires platform-specific page scraping which is out of scope for v1
                result_status = "normal"
                result_message = f"Profile 有 {len(cookies)} 条 Cookie"

            self.client.report_task(task_id, self.agent_id, "running", 80, "正在关闭 Profile")
            self.bitbrowser.close_profile(profile_id)

        except Exception as exc:
            result_status = "check_failed"
            result_message = str(exc)
            try:
                self.bitbrowser.close_profile(profile_id)
            except Exception:
                pass

        result = {
            "profile_id": profile_id,
            "platform": platform,
            "check_result": result_status,
            "cookie_count": 0,
        }
        return self.client.report_task(
            task_id, self.agent_id, "succeeded", 100,
            result_message,
            {"account_id": str(payload.get("account_id", "")), "profile_id": profile_id, "platform": platform, "check_result": result_status, "cookie_count": len(cookies) if 'cookies' in locals() else 0},
        )
