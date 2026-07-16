"""Tests for Cookie read/write executors."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wt_media_agent.executors.cookie import CookieReadExecutor, CookieWriteExecutor


class FakeClient:
    def __init__(self):
        self.reports = []

    def report_task(self, task_id, agent_id, status, progress, message=""):
        self.reports.append((task_id, status, progress, message))
        return {"status": status, "progress": progress}


class CookieExecutorTests(unittest.TestCase):
    def test_cookie_read_rejects_missing_profile(self):
        client = FakeClient()
        executor = CookieReadExecutor(client, "agent-1")
        with self.assertRaises(ValueError):
            executor.execute({"task_id": "task-1"})

    def test_cookie_read_reports_lifecycle(self):
        client = FakeClient()
        executor = CookieReadExecutor(client, "agent-1")
        # Mock bitbrowser to avoid actual API calls
        executor.bitbrowser = MagicMock()
        executor.bitbrowser.read_cookies.return_value = [{"name": "session", "value": "abc"}]

        result = executor.execute({
            "task_id": "task-1",
            "profile_id": "profile-1",
        })
        self.assertEqual(result["status"], "succeeded")
        self.assertIn("running", [r[1] for r in client.reports])
        self.assertIn("succeeded", [r[1] for r in client.reports])

    def test_cookie_write_requires_cookies(self):
        client = FakeClient()
        executor = CookieWriteExecutor(client, "agent-1")
        with self.assertRaises(ValueError):
            executor.execute({"task_id": "task-1", "profile_id": "p1"})

    def test_cookie_write_reports_lifecycle(self):
        client = FakeClient()
        executor = CookieWriteExecutor(client, "agent-1")
        executor.bitbrowser = MagicMock()
        executor.bitbrowser.read_cookies.return_value = [{"name": "session", "value": "xyz"}]

        result = executor.execute({
            "task_id": "task-1",
            "profile_id": "profile-1",
            "cookies": [{"name": "session", "value": "xyz"}],
        })
        self.assertEqual(result["status"], "succeeded")
        executor.bitbrowser.save_cookies.assert_called_once()
