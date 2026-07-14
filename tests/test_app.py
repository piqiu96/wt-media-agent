from __future__ import annotations

import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wt_media_agent.app import create_app
from wt_media_agent.local_api.server import LocalApiServer


class AgentScaffoldTests(unittest.TestCase):
    def test_create_app_runs_local_mode(self) -> None:
        app = create_app("local")

        self.assertEqual(app.mode, "local")
        self.assertEqual(app.run(), 0)

    def test_local_api_health(self) -> None:
        server = LocalApiServer()

        self.assertEqual(
            server.health(),
            {"status": "ok", "service": "wt-media-agent", "mode": "m1"},
        )

    def test_local_api_status(self) -> None:
        server = LocalApiServer()

        self.assertEqual(
            server.status(),
            {
                "agent_id": "local-agent-dev",
                "status": "idle",
                "current_task_id": None,
                "pending_result_count": 0,
            },
        )

    def test_local_api_sse_snapshot(self) -> None:
        server = LocalApiServer()

        event = server.event_stream_snapshot()

        self.assertIn("event: status", event)
        self.assertIn('"status":"idle"', event)


if __name__ == "__main__":
    unittest.main()
