"""Tests for the Local Agent HTTP/SSE control surface.

Moved out of `tests/test_app.py` (CHG-056 T-04) unchanged, then extended with
the BitBrowser-injection group: the server used to build a client from the
environment when none was passed, and `BitBrowserInjectionTests` is what keeps
that from coming back.
"""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from support import UnusedBitBrowser

from wt_media_agent.local_api import server as server_module
from wt_media_agent.local_api.server import LocalApiServer
from wt_media_agent.clients.bitbrowser import BitBrowserIdentityError


class IdentityErrorClient:
    def scan_profiles(self):
        raise BitBrowserIdentityError("no profile facts")


class BitBrowserInjectionTests(unittest.TestCase):
    """CHG-056 T-04: the server is handed its BitBrowser client, never builds one."""

    def test_the_bitbrowser_client_must_be_supplied(self) -> None:
        with self.assertRaises(TypeError):
            LocalApiServer()

    def test_the_construction_site_does_not_fall_back_to_configuration(self) -> None:
        """A missing argument must be a TypeError, not a client built from env."""
        self.assertFalse(
            hasattr(server_module, "bitbrowser_from_config"),
            "local_api must not construct a BitBrowser client",
        )

    def test_the_injected_client_is_the_one_the_server_uses(self) -> None:
        client = IdentityErrorClient()

        self.assertIs(LocalApiServer(bitbrowser=client).bitbrowser, client)

    def test_a_route_reaching_for_an_absent_browser_fails_loudly(self) -> None:
        """`UnusedBitBrowser` turns "this route needs a browser" into a failure."""
        server = LocalApiServer(bitbrowser=UnusedBitBrowser())

        with self.assertRaises(AssertionError):
            server.status()


class LocalApiServerTests(unittest.TestCase):
    def test_local_api_health(self) -> None:
        server = LocalApiServer(bitbrowser=UnusedBitBrowser())

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
        # `status()` collects environment facts through the browser client, so
        # this one needs a client that answers rather than one that refuses.
        server = LocalApiServer(bitbrowser=IdentityErrorClient())

        handler_api = server
        handler_api.state.node_id = "node-1"

        self.assertEqual(server.status()["node_id"], "node-1")

    def test_local_api_sse_snapshot(self) -> None:
        server = LocalApiServer(bitbrowser=UnusedBitBrowser())

        event = server.event_stream_snapshot()

        self.assertIn("event: status", event)
        self.assertIn('"status":"idle"', event)


if __name__ == "__main__":
    unittest.main()
