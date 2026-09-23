"""Assign a proxy to a BitBrowser Profile and verify the read-back."""

from __future__ import annotations

from collections.abc import Mapping
from wt_media_agent.clients.cloud import CloudAgentClient
from wt_media_agent.clients.bitbrowser import BitBrowserClient


class ProxyMutationExecutor:
    def __init__(
        self, client: CloudAgentClient, agent_id: str, bitbrowser: BitBrowserClient
    ) -> None:
        self.client, self.agent_id = client, agent_id
        self.bitbrowser = bitbrowser

    def execute(self, task: Mapping[str, object]) -> Mapping[str, object]:
        task_id = str(task.get("task_id", "")); payload = task.get("payload") if isinstance(task.get("payload"), Mapping) else task
        profile_id = str(payload.get("profile_id", "")); host = str(payload.get("host", ""))
        port = int(payload.get("port", 0) or 0)
        if not task_id or not profile_id or not host or port <= 0: raise ValueError("proxy_mutation_task payload invalid")
        self.client.report_task(task_id, self.agent_id, "running", 20, "正在写入 Profile 代理")
        self.bitbrowser.update_profile(profile_id, {"proxyType": payload.get("proxy_protocol", "http"), "host": host, "port": port, "proxyUserName": payload.get("username", ""), "proxyPassword": payload.get("password", "")})
        self.client.report_task(task_id, self.agent_id, "running", 70, "正在读回代理配置")
        snapshot = self.bitbrowser.scan_profiles()
        found = next((p for p in snapshot.profiles if p.bit_profile_id == profile_id), None)
        if found is None or found.proxy_host != host or found.proxy_port != port: raise RuntimeError("proxy_mutation_readback_mismatch")
        return self.client.report_task(task_id, self.agent_id, "succeeded", 100, "代理写入并读回验证成功", {"cloud_profile_id": payload.get("cloud_profile_id", ""), "proxy_id": payload.get("proxy_id", ""), "proxy_host": host, "proxy_port": port, "readback": True})
