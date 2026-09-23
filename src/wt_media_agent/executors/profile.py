"""BitBrowser Profile mutation executor with read-back-safe task reporting."""

from __future__ import annotations

from collections.abc import Mapping
import os

from wt_media_agent.clients.cloud import CloudAgentClient
from wt_media_agent.runtime.constants import DEFAULT_BITBROWSER_API_URL, DEFAULT_BITBROWSER_TIMEOUT
from wt_media_agent.clients.bitbrowser import BitBrowserClient


class ProfileMutationExecutor:
    def __init__(self, client: CloudAgentClient, agent_id: str, operation: str) -> None:
        self.client = client
        self.agent_id = agent_id
        self.operation = operation
        self.bitbrowser = BitBrowserClient(
            os.getenv("WT_MEDIA_BITBROWSER_API_URL", DEFAULT_BITBROWSER_API_URL),
            timeout=float(os.getenv("WT_MEDIA_BITBROWSER_TIMEOUT_SECONDS", str(DEFAULT_BITBROWSER_TIMEOUT))),
        )

    def execute(self, task: Mapping[str, object]) -> Mapping[str, object]:
        task_id = str(task.get("task_id", ""))
        payload = task.get("payload") if isinstance(task.get("payload"), Mapping) else task
        profile_id = str(payload.get("profile_id", ""))
        if not task_id:
            raise ValueError("profile mutation task requires task_id")
        if self.operation != "create" and not profile_id:
            raise ValueError("profile mutation task requires profile_id")

        self.client.report_task(task_id, self.agent_id, "running", 10, f"正在执行 Profile {self.operation}")
        if self.operation == "create":
            config = {str(k): v for k, v in payload.items() if k != "profile_id"}
            created_id = self.bitbrowser.create_profile(config)
            self.client.report_task(task_id, self.agent_id, "running", 70, "Profile 已创建，正在确认结果")
            snapshot = self.bitbrowser.scan_profiles()
            if not any(p.bit_profile_id == created_id for p in snapshot.profiles):
                raise RuntimeError("profile_create_readback_missing")
            return self.client.report_task(task_id, self.agent_id, "succeeded", 100, "Profile 创建并读回验证成功", {"profile_id": created_id, "readback": True})
        if self.operation == "open":
            self.bitbrowser.open_profile(profile_id)
        elif self.operation == "close":
            self.bitbrowser.close_profile(profile_id)
        elif self.operation == "update":
            config = {str(k): v for k, v in payload.items() if k != "profile_id"}
            self.bitbrowser.update_profile(profile_id, config)
        else:
            raise ValueError(f"unsupported profile operation: {self.operation}")
        self.client.report_task(task_id, self.agent_id, "running", 80, "BitBrowser 已返回成功")
        snapshot = self.bitbrowser.scan_profiles()
        if not any(p.bit_profile_id == profile_id for p in snapshot.profiles):
            raise RuntimeError("profile_mutation_readback_missing")
        return self.client.report_task(task_id, self.agent_id, "succeeded", 100, f"Profile {self.operation} 并读回验证成功", {"profile_id": profile_id, "cloud_profile_id": str(payload.get("cloud_profile_id", "")), "readback": True, "operation": self.operation})


def factory(operation: str):
    return lambda client, agent_id: ProfileMutationExecutor(client, agent_id, operation)
