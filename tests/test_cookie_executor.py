"""Tests for Cookie read/write executors.

The executors take their BitBrowser client as a mandatory constructor argument
(CHG-056 T-04). Before that they built one themselves from the environment,
which left the tests no seam: they constructed the executor and then reached in
to overwrite `executor.bitbrowser` with a mock. The injection tests below are
what make that impossible to reintroduce -- a two-argument construction has to
fail loudly rather than quietly build a real client.
"""

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


def bitbrowser_with_cookies(cookies: list[dict[str, object]]) -> MagicMock:
    """A stand-in for the injected BitBrowser client."""
    bitbrowser = MagicMock()
    bitbrowser.read_cookies.return_value = cookies
    return bitbrowser


class CookieExecutorInjectionTests(unittest.TestCase):
    def test_the_read_executor_uses_the_injected_client(self):
        bitbrowser = bitbrowser_with_cookies([{"name": "session", "value": "abc"}])

        executor = CookieReadExecutor(FakeClient(), "agent-1", bitbrowser)

        self.assertIs(executor.bitbrowser, bitbrowser)

    def test_the_write_executor_uses_the_injected_client(self):
        bitbrowser = bitbrowser_with_cookies([{"name": "session", "value": "xyz"}])

        executor = CookieWriteExecutor(FakeClient(), "agent-1", bitbrowser)

        self.assertIs(executor.bitbrowser, bitbrowser)

    def test_the_client_argument_is_mandatory(self):
        """No default means no silent self-construction."""
        for factory in (CookieReadExecutor, CookieWriteExecutor):
            with self.subTest(factory=factory.__name__):
                with self.assertRaises(TypeError):
                    factory(FakeClient(), "agent-1")


class CookieExecutorTests(unittest.TestCase):
    def test_cookie_read_rejects_missing_profile(self):
        client = FakeClient()
        executor = CookieReadExecutor(client, "agent-1", bitbrowser_with_cookies([]))
        with self.assertRaises(ValueError):
            executor.execute({"task_id": "task-1"})

    def test_cookie_read_reports_lifecycle(self):
        client = FakeClient()
        bitbrowser = bitbrowser_with_cookies([{"name": "session", "value": "abc"}])
        executor = CookieReadExecutor(client, "agent-1", bitbrowser)

        result = executor.execute({
            "task_id": "task-1",
            "profile_id": "profile-1",
        })
        self.assertEqual(result["status"], "succeeded")
        self.assertIn("running", [r[1] for r in client.reports])
        self.assertIn("succeeded", [r[1] for r in client.reports])

    def test_cookie_write_requires_cookies(self):
        client = FakeClient()
        executor = CookieWriteExecutor(client, "agent-1", bitbrowser_with_cookies([]))
        with self.assertRaises(ValueError):
            executor.execute({"task_id": "task-1", "profile_id": "p1"})

    def test_cookie_write_reports_lifecycle(self):
        client = FakeClient()
        bitbrowser = bitbrowser_with_cookies([{"name": "session", "value": "xyz"}])
        executor = CookieWriteExecutor(client, "agent-1", bitbrowser)

        result = executor.execute({
            "task_id": "task-1",
            "profile_id": "profile-1",
            "cookies": [{"name": "session", "value": "xyz"}],
        })
        self.assertEqual(result["status"], "succeeded")
        bitbrowser.save_cookies.assert_called_once()


if __name__ == "__main__":
    unittest.main()
