"""Cloud discovery task executor backed by the Douyin channel adapter."""

from __future__ import annotations

from collections.abc import Mapping

from wt_media_agent.adapters.douyin import DouyinAdapter
from wt_media_agent.cloud_agent_client import CloudAgentClient


class DiscoveryExecutor:
    def __init__(self, client: CloudAgentClient, agent_id: str, adapter: DouyinAdapter | None = None) -> None:
        self.client = client
        self.agent_id = agent_id
        self.adapter = adapter or DouyinAdapter()

    def execute(self, task: Mapping[str, object]) -> Mapping[str, object]:
        task_id = str(task.get("task_id") or "")
        if not task_id or task.get("task_type") != "discovery_task":
            raise ValueError("discovery executor requires discovery_task with task_id")
        payload = task.get("payload") if isinstance(task.get("payload"), Mapping) else task
        operation = str(payload.get("operation") or "")
        self.client.report_task(task_id, self.agent_id, "running", 10, "正在连接渠道")
        if operation == "url":
            raw_urls = payload.get("urls")
            urls = [str(value).strip() for value in raw_urls if str(value).strip()] if isinstance(raw_urls, list) else [str(payload.get("url") or "").strip()]
            items = []
            for url in urls:
                item = self.adapter.fetch_by_url(url)
                if item:
                    items.append(item)
        elif operation == "keyword":
            keywords = payload.get("keywords")
            if isinstance(keywords, list):
                items = []
                for keyword in keywords:
                    items.extend(self.adapter.search(str(keyword), limit=int(payload.get("limit") or 20), offset=int(payload.get("offset") or 0)))
            else:
                items = self.adapter.search(str(payload.get("keyword") or ""), limit=int(payload.get("limit") or 20), offset=int(payload.get("offset") or 0))
        elif operation == "author":
            items = self.adapter.author_posts(str(payload.get("author") or payload.get("author_id") or ""), limit=int(payload.get("limit") or 20), offset=int(payload.get("offset") or 0))
        else:
            raise ValueError("unsupported discovery operation")
        result = {"crawl_task_id": payload.get("crawl_task_id"), "items": items, "scanned": len(items), "found": len(items)}
        return self.client.report_task(task_id, self.agent_id, "succeeded", 100, "渠道发现完成", result)
