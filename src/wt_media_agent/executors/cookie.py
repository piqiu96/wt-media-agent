"""Cookie read/write executors for BitBrowser Profile cookie management."""

from __future__ import annotations

from collections.abc import Mapping

import os

from wt_media_agent.cloud_agent_client import CloudAgentClient
from wt_media_agent.runtime.constants import (
    DEFAULT_BITBROWSER_API_URL,
    DEFAULT_BITBROWSER_TIMEOUT,
)
from wt_media_agent.clients.bitbrowser import BitBrowserClient


class CookieReadExecutor:
    """Read cookies from a BitBrowser Profile and report back."""

    def __init__(self, client: CloudAgentClient, agent_id: str) -> None:
        self.client = client
        self.agent_id = agent_id
        bb_url = os.getenv("WT_MEDIA_BITBROWSER_API_URL", DEFAULT_BITBROWSER_API_URL)
        bb_timeout = float(os.getenv("WT_MEDIA_BITBROWSER_TIMEOUT_SECONDS", str(DEFAULT_BITBROWSER_TIMEOUT)))
        self.bitbrowser = BitBrowserClient(bb_url, timeout=bb_timeout)

    def execute(self, task: Mapping[str, object]) -> Mapping[str, object]:
        task_id = task.get("task_id", "")
        payload = task.get("payload") if isinstance(task.get("payload"), Mapping) else task
        profile_id = str(payload.get("profile_id", "") or payload.get("browser_profile_id", ""))
        account_id = str(payload.get("account_id", ""))
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("cookie_read_task requires task_id")
        if not profile_id:
            raise ValueError("cookie_read_task requires profile_id or browser_profile_id")

        self.client.report_task(str(task_id), self.agent_id, "running", 10, "正在打开 Profile")
        self.bitbrowser.open_profile(profile_id)

        self.client.report_task(str(task_id), self.agent_id, "running", 50, "正在读取 Cookie")
        cookies = self.bitbrowser.read_cookies(profile_id)

        self.client.report_task(str(task_id), self.agent_id, "running", 80, "正在关闭 Profile")
        self.bitbrowser.close_profile(profile_id)

        result = {
            "profile_id": profile_id,
            "cookie_count": len(cookies),
        }
        if account_id:
            result["account_id"] = account_id
        return _report_with_result(
            self.client,
            str(task_id), self.agent_id, "succeeded", 100,
            f"读取到 {len(cookies)} 条 Cookie",
            result,
        )


class CookieWriteExecutor:
    """Write cookies to a BitBrowser Profile and verify."""

    def __init__(self, client: CloudAgentClient, agent_id: str) -> None:
        self.client = client
        self.agent_id = agent_id
        bb_url = os.getenv("WT_MEDIA_BITBROWSER_API_URL", DEFAULT_BITBROWSER_API_URL)
        bb_timeout = float(os.getenv("WT_MEDIA_BITBROWSER_TIMEOUT_SECONDS", str(DEFAULT_BITBROWSER_TIMEOUT)))
        self.bitbrowser = BitBrowserClient(bb_url, timeout=bb_timeout)

    def execute(self, task: Mapping[str, object]) -> Mapping[str, object]:
        task_id = task.get("task_id", "")
        payload = task.get("payload") if isinstance(task.get("payload"), Mapping) else task
        profile_id = str(payload.get("profile_id", "") or payload.get("browser_profile_id", ""))
        account_id = str(payload.get("account_id", ""))
        cookies = payload.get("cookies", [])
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("cookie_write_task requires task_id")
        if not profile_id:
            raise ValueError("cookie_write_task requires profile_id")
        if not isinstance(cookies, list) or not cookies:
            raise ValueError("cookie_write_task requires a non-empty cookies list")

        self.client.report_task(str(task_id), self.agent_id, "running", 10, "正在打开 Profile")
        self.bitbrowser.open_profile(profile_id)

        self.client.report_task(str(task_id), self.agent_id, "running", 50, f"正在写入 {len(cookies)} 条 Cookie")
        self.bitbrowser.save_cookies(profile_id, cookies)

        self.client.report_task(str(task_id), self.agent_id, "running", 80, "正在验证写入")
        read_back = self.bitbrowser.read_cookies(profile_id)

        self.bitbrowser.close_profile(profile_id)

        result = {
            "profile_id": profile_id,
            "written": len(cookies),
            "verified_count": len(read_back),
        }
        if account_id:
            result["account_id"] = account_id
        return _report_with_result(
            self.client,
            str(task_id), self.agent_id, "succeeded", 100,
            f"已写入 {len(cookies)} 条 Cookie，验证到 {len(read_back)} 条",
            result,
        )


def _report_with_result(
    client: CloudAgentClient,
    task_id: str,
    agent_id: str,
    status: str,
    progress: int,
    message: str,
    result: Mapping[str, object],
) -> Mapping[str, object]:
    """Report structured metadata while retaining compatibility with test doubles."""
    try:
        return client.report_task(
            task_id, agent_id, status, progress, message, result=result
        )
    except TypeError as exc:
        if "result" not in str(exc):
            raise
        return client.report_task(task_id, agent_id, status, progress, message)
