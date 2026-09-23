"""Agent-side proxy connectivity verification."""

from __future__ import annotations

from collections.abc import Mapping

from wt_media_agent.clients.cloud import CloudAgentClient
from wt_media_agent.services.net.proxy import check_proxy_connectivity


class ProxyCheckExecutor:
    def __init__(self, client: CloudAgentClient, agent_id: str) -> None:
        self.client = client
        self.agent_id = agent_id

    def execute(self, task: Mapping[str, object]) -> Mapping[str, object]:
        task_id = str(task.get("task_id", ""))
        payload = task.get("payload") if isinstance(task.get("payload"), Mapping) else task
        host = str(payload.get("host", ""))
        try:
            port = int(payload.get("port", 0))
        except (TypeError, ValueError):
            port = 0
        if not task_id or not host or port <= 0:
            raise ValueError("proxy_check_task requires task_id, host, and port")
        self.client.report_task(task_id, self.agent_id, "running", 20, "正在验证代理连通性")
        result = check_proxy_connectivity(host, port)
        return self.client.report_task(task_id, self.agent_id, "succeeded", 100, result, {"proxy_id": str(payload.get("proxy_id", "")), "connectivity": result})
