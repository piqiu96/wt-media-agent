import unittest

from wt_media_agent.executors.discovery import DiscoveryExecutor


class Client:
    def __init__(self):
        self.reports = []

    def report_task(self, task_id, agent_id, status, progress, message="", result=None):
        self.reports.append((task_id, status, progress, message, result))
        return {"task_id": task_id, "status": status, "result": result or {}}


class Adapter:
    def search(self, keyword, **kwargs):
        return [{"platform_content_id": "a1", "title": keyword}]

    def fetch_by_url(self, url):
        return {"platform_content_id": url, "title": url}


class DiscoveryExecutorTests(unittest.TestCase):
    def test_keyword_task_reports_normalized_result(self):
        client = Client()
        result = DiscoveryExecutor(client, "agent-1", Adapter()).execute({"task_id": "task-1", "task_type": "discovery_task", "payload": {"operation": "keyword", "keyword": "demo", "crawl_task_id": 7}})
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(client.reports[-1][4]["crawl_task_id"], 7)
        self.assertEqual(client.reports[-1][4]["items"][0]["platform_content_id"], "a1")

    def test_url_task_supports_batch_links(self):
        client = Client()
        result = DiscoveryExecutor(client, "agent-1", Adapter()).execute({"task_id": "task-2", "task_type": "discovery_task", "payload": {"operation": "url", "urls": ["u1", "u2"], "crawl_task_id": 8}})
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual([item["platform_content_id"] for item in client.reports[-1][4]["items"]], ["u1", "u2"])
