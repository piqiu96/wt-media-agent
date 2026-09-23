"""Tests for the Local Agent HTTP/SSE control surface.

Moved out of `tests/test_app.py` (CHG-056 T-04) so that the tests of the local
API outlive the `app.py` scaffold they happened to share a file with. Contents
are unchanged by the move.
"""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wt_media_agent.local_api.server import LocalApiServer
from wt_media_agent.clients.bitbrowser import BitBrowserIdentityError


class IdentityErrorClient:
    def scan_profiles(self):
        raise BitBrowserIdentityError("no profile facts")


class LocalApiServerTests(unittest.TestCase):
    def test_local_api_health(self) -> None:
        server = LocalApiServer()

        self.assertEqual(
            server.health(),
            {"status": "ok", "service": "wt-media-agent", "mode": "m1"},
        )

    def test_local_api_status(self) -> None:
        server = LocalApiServer(bitbrowser=IdentityErrorClient())

        status = server.status()
        self.assertEqual(status["node_id"], "")
        self.assertEqual(status["agent_id"], "local-agent-dev")
        self.assertEqual(status["status"], "idle")
        self.assertEqual(status["current_task_id"], None)
        self.assertEqual(status["pending_result_count"], 0)
        self.assertEqual(status["bitbrowser_status"], "identity_unverifiable")
        self.assertNotIn("main_user_id", status)

    def test_local_bind_projects_node_id_into_status(self) -> None:
        server = LocalApiServer()

        handler_api = server
        handler_api.state.node_id = "node-1"

        self.assertEqual(server.status()["node_id"], "node-1")

    def test_local_api_sse_snapshot(self) -> None:
        server = LocalApiServer()

        event = server.event_stream_snapshot()

        self.assertIn("event: status", event)
        self.assertIn('"status":"idle"', event)


if __name__ == "__main__":
    unittest.main()
