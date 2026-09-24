"""Tests for the Local Agent HTTP/SSE control surface.

Moved out of `tests/test_app.py` (CHG-056 T-04) unchanged, then extended with
the BitBrowser-injection group: the server used to build a client from the
environment when none was passed, and `BitBrowserInjectionTests` is what keeps
that from coming back.
"""

from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from support import UnusedBitBrowser

from wt_media_agent.local_api import server as server_module
from wt_media_agent.local_api.server import LocalApiServer
from wt_media_agent.local_api.state import LocalAgentState
from wt_media_agent.clients.bitbrowser import BitBrowserIdentityError


class IdentityErrorClient:
    def scan_profiles(self):
        raise BitBrowserIdentityError("no profile facts")


class BitBrowserInjectionTests(unittest.TestCase):
    """CHG-056 T-04: the server is handed its BitBrowser client, never builds one."""

    def test_the_bitbrowser_client_must_be_supplied(self) -> None:
        """The state is passed so the TypeError can only be about the browser.

        T-09 made `state` mandatory too, and `LocalApiServer()` alone would then
        raise for *that* missing argument -- leaving this test green while it
        stopped testing what its name says.
        """
        with self.assertRaises(TypeError):
            LocalApiServer(LocalAgentState())

    def test_the_construction_site_does_not_fall_back_to_configuration(self) -> None:
        """A missing argument must be a TypeError, not a client built from env."""
        self.assertFalse(
            hasattr(server_module, "bitbrowser_from_config"),
            "local_api must not construct a BitBrowser client",
        )

    def test_the_injected_client_is_the_one_the_server_uses(self) -> None:
        client = IdentityErrorClient()

        self.assertIs(LocalApiServer(LocalAgentState(), bitbrowser=client).bitbrowser, client)

    def test_a_route_reaching_for_an_absent_browser_fails_loudly(self) -> None:
        """`UnusedBitBrowser` turns "this route needs a browser" into a failure."""
        server = LocalApiServer(LocalAgentState(), bitbrowser=UnusedBitBrowser())

        with self.assertRaises(AssertionError):
            server.status()


class AgentStateInjectionTests(unittest.TestCase):
    """T-09: the state the server reports is the one it was handed.

    `__init__` used to read `state or LocalAgentState()`, so a caller that
    forgot the argument got a fresh, plausible-looking state -- `agent_id`
    "local-agent-dev", `status` "idle" -- and `/api/v1/status` would have
    described an agent that was not the one running. The default is gone; these
    tests keep the silence from coming back. (What made it silent is that
    `LocalAgentState` is a plain dataclass: no `__bool__`, no `__len__`, so a
    *passed* state was never discarded -- the failure mode was a forgotten
    argument, never a falsy one.)
    """

    def test_the_state_must_be_supplied(self) -> None:
        """No state, a browser supplied: the TypeError can only be the state."""
        with self.assertRaises(TypeError):
            LocalApiServer(bitbrowser=UnusedBitBrowser())

    def test_serve_requires_the_state_it_serves(self) -> None:
        """`serve` had the same default one level up; it is gone too.

        Checked on the signature rather than by calling, and deliberately: while
        the default is still there a call *succeeds* into
        `ThreadingHTTPServer(("127.0.0.1", 8765))` -- the developer's running dev
        Agent port. A red run must not reach for that port, and a call on a
        scratch port would instead block in `serve_forever`. The fact at issue
        is "there is no default", and that is what this reads.
        """
        parameter = inspect.signature(server_module.serve).parameters["state"]
        self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_the_injected_state_is_the_one_the_server_reports(self) -> None:
        state = LocalAgentState()
        state.agent_id = "t09-agent"
        state.status = "running"

        # A browser that answers, because `status()` asks it for facts; the
        # assertion is about which state object the answer came from.
        server = LocalApiServer(state, bitbrowser=IdentityErrorClient())

        self.assertIs(server.state, state)
        self.assertEqual(server.status()["agent_id"], "t09-agent")


class LocalApiServerTests(unittest.TestCase):
    def test_local_api_health(self) -> None:
        server = LocalApiServer(LocalAgentState(), bitbrowser=UnusedBitBrowser())

        self.assertEqual(
            server.health(),
            {"status": "ok", "service": "wt-media-agent", "mode": "m1"},
        )

    def test_local_api_status(self) -> None:
        server = LocalApiServer(LocalAgentState(), bitbrowser=IdentityErrorClient())

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
        server = LocalApiServer(LocalAgentState(), bitbrowser=IdentityErrorClient())

        handler_api = server
        handler_api.state.node_id = "node-1"

        self.assertEqual(server.status()["node_id"], "node-1")

    def test_local_api_sse_snapshot(self) -> None:
        server = LocalApiServer(LocalAgentState(), bitbrowser=UnusedBitBrowser())

        event = server.event_stream_snapshot()

        self.assertIn("event: status", event)
        self.assertIn('"status":"idle"', event)


class LoggerNameTests(unittest.TestCase):
    """T-09: the module's records are named after the component, however it started.

    `scripts/verify-health.sh` and `scripts/start-health.sh` both run
    `python -m wt_media_agent.local_api.server`. Under `-m` the module executes
    as `__main__`, so `getLogger(__name__)` named the logger `__main__` and every
    HTTP-side record lost the component it came from -- the opposite of what
    T-04's routing and the `wt_media_agent` component prefix are for. T-07
    measured it and registered it here.

    These two are a pair because neither sees the whole thing: the first reads
    the live logger (import mode only), the second reads the source, which is
    where the `-m` mode differs. The real `-m` process is measured in
    `evidence/task-09-single-entry.md`.
    """

    def test_the_logger_is_named_after_the_module(self) -> None:
        self.assertEqual(server_module.logger.name, "wt_media_agent.local_api.server")

    def test_the_logger_name_does_not_come_from___name__(self) -> None:
        """No `__name__` anywhere inside the `getLogger` call.

        Anywhere inside, not just as the whole argument: the name it produces
        on the import path is the module path either way, so only the `-m` mode
        tells them apart, and that is the mode the imported module cannot show.
        """
        source = (
            Path(__file__).resolve().parents[1]
            / "src" / "wt_media_agent" / "local_api" / "server.py"
        ).read_text()

        calls = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "getLogger"
        ]
        self.assertEqual(len(calls), 1, "expected exactly one getLogger call to check")
        names = {node.id for node in ast.walk(calls[0]) if isinstance(node, ast.Name)}
        self.assertNotIn("__name__", names, "getLogger must not take __name__: under -m it is `__main__`")


if __name__ == "__main__":
    unittest.main()
