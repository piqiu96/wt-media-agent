"""Tests for the Local Agent HTTP/SSE control surface.

Moved out of `tests/test_app.py` (CHG-056 T-04) unchanged, then extended with
the BitBrowser-injection group: the server used to build a client from the
environment when none was passed, and `BitBrowserInjectionTests` is what keeps
that from coming back.
"""

from __future__ import annotations

import ast
import http.client
import inspect
import json
import re
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from support import UnusedBitBrowser

from wt_media_agent.local_api import server as server_module
from wt_media_agent.local_api.server import LocalApiServer, make_handler
from wt_media_agent.local_api.state import LocalAgentState
from wt_media_agent.clients.bitbrowser import BitBrowserIdentityError, BitProfile, ProfileSnapshot


class IdentityErrorClient:
    def scan_profiles(self):
        raise BitBrowserIdentityError("no profile facts")


class CountingClient:
    """A scanner that answers, and says how many times it was asked."""

    def __init__(self) -> None:
        self.calls = 0

    def scan_profiles(self) -> ProfileSnapshot:
        self.calls += 1
        return ProfileSnapshot(
            main_user_id=f"main-user-{self.calls}",
            profiles=(BitProfile("profile-1", "bit-user-1", "main-user-1", "Account 1", 1, "", "", 1, "", "", "noproxy", "", 0),),
        )


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


class StatusScanParameterTests(unittest.TestCase):
    """`?scan=reuse` is the only way to ask for a cached BitBrowser scan.

    Driven over a real socket rather than by calling `status()` directly: what is
    at issue is whether the *query string* reaches the server at all, and a
    server that ignores it looks identical from the method call.
    """

    def setUp(self) -> None:
        self.client = CountingClient()
        self.server = LocalApiServer(LocalAgentState(), bitbrowser=self.client)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.server))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def _get(self, path: str) -> dict:
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=10)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            self.assertEqual(response.status, 200, path)
            return json.loads(response.read())
        finally:
            connection.close()

    def test_the_default_and_live_always_scan(self) -> None:
        """An existing caller keeps the behavior it has today: a fresh scan."""
        self._get("/api/v1/status")
        self._get("/api/v1/status?scan=live")

        self.assertEqual(self.client.calls, 2)

    def test_reuse_serves_the_first_scan_to_the_second_request(self) -> None:
        first = self._get("/api/v1/status?scan=reuse")
        second = self._get("/api/v1/status?scan=reuse")

        self.assertEqual(self.client.calls, 1)
        # `disk` is measured live on every request, so comparing it would make
        # this a disk-churn detector: any write landing between the two calls
        # moves free_megabytes and the comparison fails, on a machine the cache
        # behaved correctly on. The scan facts are what reuse governs.
        for response in (first, second):
            response["data"].pop("disk")
        self.assertEqual(second, first)

    def test_a_live_scan_does_not_satisfy_a_later_reuse(self) -> None:
        """Only the reuse path and the explicit scan route fill the cache.

        Pinned rather than incidental: it is what makes the first background tick
        after a manual check still scan, instead of the two paths quietly
        depending on one another.
        """
        self._get("/api/v1/status")
        self._get("/api/v1/status?scan=reuse")
        self._get("/api/v1/status?scan=reuse")

        self.assertEqual(self.client.calls, 2)

    def test_an_unknown_scan_value_behaves_like_the_default(self) -> None:
        """Only an explicit `reuse` is allowed to reuse; anything else is live."""
        self._get("/api/v1/status?scan=Reuse")
        self._get("/api/v1/status?scan=1")

        self.assertEqual(self.client.calls, 2)

    def test_the_explicit_scan_route_fills_the_cache(self) -> None:
        self._post("/api/v1/bit-browser/profile-scans")
        self._get("/api/v1/status?scan=reuse")

        self.assertEqual(self.client.calls, 1)

    def _post(self, path: str) -> dict:
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=10)
        try:
            connection.request("POST", path)
            response = connection.getresponse()
            self.assertEqual(response.status, 200, path)
            return json.loads(response.read())
        finally:
            connection.close()


class LoggerNameTests(unittest.TestCase):
    """T-09: the module's records are named after the component, however it started.

    `scripts/verify-health.sh` and `bin/control.sh` both run
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


#: A path key in the frozen contract's `paths:` mapping, at its two-space indent.
CONTRACT_PATH = re.compile(r"^  (/[^\s:]*):")
#: An operation key nested one level under a path key.
CONTRACT_METHOD = re.compile(r"^    (get|post|put|patch|delete):")
#: Where `paths:` ends and the schema definitions begin.
CONTRACT_COMPONENTS = "components:"


def _contract_operations() -> set[tuple[str, str]]:
    """Every `(METHOD, path)` the frozen Local Agent API promises."""
    text = (
        Path(__file__).resolve().parents[1]
        / "contracts" / "local-agent-api" / "v1" / "local-agent.openapi.yaml"
    ).read_text()

    operations: set[tuple[str, str]] = set()
    path: str | None = None
    in_paths = False
    for line in text.splitlines():
        if line.startswith("paths:"):
            in_paths = True
            continue
        if in_paths and line.startswith(CONTRACT_COMPONENTS):
            break
        if not in_paths:
            continue
        found_path = CONTRACT_PATH.match(line)
        if found_path:
            path = found_path.group(1)
            continue
        found_method = CONTRACT_METHOD.match(line)
        if found_method and path is not None:
            operations.add((found_method.group(1).upper(), path))
    return operations


def _served_operations() -> set[tuple[str, str]]:
    """Every `(METHOD, path)` the handler's `if self.path ==` chains answer.

    Read out of the AST rather than by calling the routes: one of them is an SSE
    stream that never returns, and a check that cannot include it would report a
    contract route as missing for the wrong reason.
    """
    tree = ast.parse(
        (
            Path(__file__).resolve().parents[1]
            / "src" / "wt_media_agent" / "local_api" / "server.py"
        ).read_text()
    )

    served: set[tuple[str, str]] = set()
    for handler in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for method in handler.body:
            if not isinstance(method, ast.FunctionDef) or not method.name.startswith("do_"):
                continue
            http_method = method.name[len("do_"):]
            for node in ast.walk(method):
                if not isinstance(node, ast.Compare) or len(node.ops) != 1:
                    continue
                if not isinstance(node.ops[0], ast.Eq) or len(node.comparators) != 1:
                    continue
                left, right = node.left, node.comparators[0]
                if not (
                    isinstance(left, ast.Attribute)
                    and left.attr == "path"
                    and isinstance(right, ast.Constant)
                    and isinstance(right.value, str)
                ):
                    continue
                served.add((http_method, right.value))
    return served


class ContractRouteTests(unittest.TestCase):
    """Every path the frozen contract promises is a path this server answers.

    The contract is what the Desktop codes against, and the two files are
    edited in different repositories: a path added to the OpenAPI with no branch
    behind it reads as a working endpoint and answers `404 not_found`, which is
    a shape nothing else in the suite distinguishes from a typo in a caller.
    Both directions were unequal long before this test -- the server also serves
    paths the contract never listed -- so this asserts the inclusion the
    contract actually claims, and reports the missing ones by name.
    """

    def test_every_promised_path_has_a_branch(self) -> None:
        promised = _contract_operations()

        self.assertGreaterEqual(
            len(promised), 8, f"the contract was read as {sorted(promised)}"
        )
        self.assertEqual(promised - _served_operations(), set())

    def test_the_reader_can_see_a_path_that_is_not_there(self) -> None:
        """A positive control, because a mis-parsed contract reads as `set()`.

        An empty `promised` would satisfy the inclusion above while checking
        nothing, and the guard would be green for the rest of its life.
        """
        served = _served_operations()

        self.assertIn(("GET", "/api/v1/save-directory"), served)
        self.assertNotIn(("DELETE", "/api/v1/save-directory"), served)


#: Where the region keeps the vocabulary for this API's refusals.
TRANSFER_ERROR_CODES = ROOT / "contracts" / "local-error-codes" / "v1" / "transfer.yaml"
#: A declared refusal and the status it is returned with, at its two-space indent.
DECLARED_ERROR = re.compile(r"^  ([a-z_]+):\s*\{http_status:\s*(\d+)\}", re.M)
#: Where `errors:` ends and the executor's terminal reasons begin. Those are the
#: other vocabulary in the same file, and it is not this test's to keep.
EXECUTOR_ERRORS_KEY = "executor_errors:"
#: What marks a refusal as belonging to this file's scope. The region holds one
#: file per area and the areas are told apart by their names, so a transfer-area
#: refusal named outside these prefixes escapes the comparison below -- the naming
#: is what carries the scope, and renaming one out of it is the way to break this.
#:
#: A tuple because the transfer area has grown past one route: the save-directory
#: pair and the bind route's credential answer are different subjects, and they
#: are in this file together because they are what CHG-061's download path added.
#: Widening this is the one edit that can hide an undeclared refusal again, so it
#: is widened only when a route the same change adds needs it -- never to silence
#: a name that appeared elsewhere (the nine M2-era ones are registered in
#: `delivery/active/CHG-20260924-061/checkpoint.md` instead).
TRANSFER_CODE_PREFIXES = ("save_directory_", "node_credential_")


def _refusal_of(node: ast.AST) -> tuple[str, int] | None:
    """The `(code, status)` a `return <status>, {"error": {"code": <code>}}` names."""
    if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Tuple):
        return None
    if len(node.value.elts) != 2:
        return None
    status, envelope = node.value.elts
    if not isinstance(status, ast.Constant) or not isinstance(status.value, int):
        return None
    if not isinstance(envelope, ast.Dict) or len(envelope.keys) != 1:
        return None
    if not isinstance(envelope.keys[0], ast.Constant) or envelope.keys[0].value != "error":
        return None
    inner = envelope.values[0]
    if not isinstance(inner, ast.Dict):
        return None
    for key, value in zip(inner.keys, inner.values):
        if not (isinstance(key, ast.Constant) and key.value == "code"):
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value, status.value
    return None


def _served_refusals() -> dict[str, int]:
    """Every refusal this API can answer with, keyed by code.

    Read out of the AST rather than by calling the routes: reaching some of these
    takes a server assembled without a database, and a check that had to build
    every such server would test the setup more than the vocabulary.
    """
    tree = ast.parse((ROOT / "src" / "wt_media_agent" / "local_api" / "server.py").read_text())

    refusals: dict[str, int] = {}
    for node in ast.walk(tree):
        found = _refusal_of(node)
        if found is not None:
            refusals[found[0]] = found[1]
    return refusals


def _declared_errors() -> dict[str, int]:
    """Every refusal `transfer.yaml` declares, with the status it declares."""
    text = TRANSFER_ERROR_CODES.read_text()
    block = text.split(EXECUTOR_ERRORS_KEY)[0]
    return {name: int(status) for name, status in DECLARED_ERROR.findall(block)}


class ContractCodeTests(unittest.TestCase):
    """Every refusal this API answers with is one the vocabulary declares.

    The region's own README says `errors` is what the local API returns, and the
    two are edited in different files by different changes: a refusal added to a
    route with no line in the vocabulary is a status the Desktop and the log
    reader have no name for, and nothing else in the suite distinguishes it from
    a code somebody forgot to look up.

    Both directions are asserted, because both are failures: a code the module
    returns and the file does not name leaves a caller without a meaning, and a
    name the module never returns is a promise about behaviour that does not
    exist. What is compared is `(code, status)` and not the code alone -- the
    status is what the caller acts on, and moving a refusal from `400` to `503`
    without saying so is the same defect as renaming it.
    """

    def test_every_refusal_is_declared_with_the_status_it_is_returned_with(self) -> None:
        declared = _declared_errors()
        self.assertTrue(declared, "the vocabulary was read as empty")

        served = {
            code: status
            for code, status in _served_refusals().items()
            if code.startswith(TRANSFER_CODE_PREFIXES)
        }

        self.assertEqual(served, declared)

    def test_the_reader_can_see_a_refusal_that_is_there(self) -> None:
        """A positive control: a mis-read module and a mis-read file both give `{}`.

        Two empty mappings compare equal, so without this the guard above would
        pass while checking nothing -- which is exactly the state the vocabulary
        file was in before it had a consumer.
        """
        served = _served_refusals()
        self.assertEqual(served["save_directory_invalid"], 400)
        self.assertEqual(served["save_directory_unavailable"], 503)
        self.assertEqual(served["node_credential_unavailable"], 503)
        self.assertEqual(_declared_errors()["save_directory_invalid"], 400)
        self.assertEqual(_declared_errors()["node_credential_unavailable"], 503)


if __name__ == "__main__":
    unittest.main()
