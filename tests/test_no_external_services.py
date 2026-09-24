"""Tests must not reach the real network (CHG-057 T-19).

`delivery/milestones/M-launch-engineering.md` names "测试不误连真实外部服务" as a
failure behaviour, and until this file nothing enforced it: the rule was a
convention, so a new test could construct a real `BitBrowserClient` against
whatever URL and nothing would say a word until someone ran the suite on a
machine where `:54345` happened to be up -- and then it would pass, having
talked to the developer's browser.

Two shapes reach out of the process, and both are checked here:

- a client built without a fake `transport`, which falls back to the real HTTP
  one (`CloudAgentClient` reaches `urlopen`; `BitBrowserClient` asks its
  factory for a transport);
- a `urlopen` call that no `patch` in its own function replaces.

The escape is deliberate and visible: a line marked `# network-ok: <reason>`
is not a violation, and the reason must be non-empty. Five sites use it today,
each because the real transport is the subject of the test or because the URL
points at an HTTP server that same function just started. `NETWORK_OK_SITES`
freezes them, so adding one and removing one are both edits to this file -- an
escape hatch nobody has to look at is the thing that gets abused.

What this file cannot check, and where the risk goes instead:

- reachability through a helper: `test_cloud_agent_client.py` builds a client
  with no transport and calls it from `_call`, which patches `urlopen`. That is
  correct code, and this rule would report it, so it carries a marker whose
  reason names the helper. If the helper stopped patching, this file would not
  notice -- the marker is a pointer for a reader, not a proof.
- a URL that is external but never called: only the construction is judged, so
  a client pointed at a public host and never used passes.
- `os.system`, raw `socket`, subprocess, or a client factory reached by another
  name -- the same "modules dispatched at runtime" limit
  `test_dependency_boundaries.py` documents for its own rules.
- whether a patched transport is *used*: `transport=lambda ...: {}` is accepted
  on its shape, not on whether the test's assertion is meaningful.
"""

from __future__ import annotations

import ast
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

#: The client classes whose real transport reaches the network. Constructing
#: one without a `transport` argument is the thing this file exists to see.
NETWORK_CLIENTS = ("BitBrowserClient", "CloudAgentClient")

#: The escape marker, and the reason it must carry. The marker may sit on the
#: call's own line or on the line above it, because some of the reasons do not
#: fit in a trailing comment.
MARKER = "# network-ok:"

#: How many markers each file carries. Frozen on purpose (see the docstring)
#: and counted per file rather than by line number: a line number would go
#: stale on any edit above it, and then the ratchet would fail for a reason
#: nobody could act on.
NETWORK_OK_COUNTS = {
    "test_bootstrap.py": 1,
    "test_cloud_agent_client.py": 2,
    "test_local_health.py": 1,
    "test_proxy_extract.py": 1,
}

#: This file carries the marker as a *string* -- the constant above, the
#: docstring, the controls' planted sources -- so it would find its own text and
#: report it. It is excluded from the marker count for that reason only; the
#: violation scan still reads it, and correctly finds nothing, because a marker
#: inside a string literal is a `Constant` and not a call.
MARKER_SOURCE = Path(__file__).name

#: Floor for discovery, so a scan that stops finding anything fails here rather
#: than passing as "no violations".
KNOWN_CONSTRUCTIONS = 35
KNOWN_MODULES = 30


def test_files() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(TESTS.glob("test_*.py"))}


def _source_lines(source: str) -> list[str]:
    return source.splitlines()


def _enclosing(tree: ast.AST) -> dict[int, ast.AST]:
    """`{child node id: parent}` for the whole tree."""
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


def _enclosing_function(tree: ast.AST, node: ast.AST) -> ast.AST | None:
    parents = _enclosing(tree)
    current = parents.get(id(node))
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = parents.get(id(current))
    return None


def _patches_urlopen(function: ast.AST) -> bool:
    """Does this function replace `urlopen` -- by decorator or by a `with`?"""
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if called not in ("patch", "patch.object"):
            continue
        if any(
            isinstance(argument, ast.Constant) and isinstance(argument.value, str) and "urlopen" in argument.value
            for argument in list(node.args) + [keyword.value for keyword in node.keywords]
        ):
            return True
    return False


def _own_scope(tree: ast.AST) -> list[ast.AST]:
    """Module-level nodes, without descending into any function body.

    `ast.walk` would carry a `with ... as urlopen` out of some unrelated
    function and use it to excuse a bare `urlopen()` at module level. A scope
    is what the name is visible in, and a sibling function is not it.
    """
    nodes: list[ast.AST] = []
    queue = list(ast.iter_child_nodes(tree))
    while queue:
        node = queue.pop()
        # Not even the def itself: its parameter list is that function's
        # scope, and a parameter named `urlopen` says nothing about a call
        # made at module level.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        nodes.append(node)
        queue.extend(ast.iter_child_nodes(node))
    return nodes


def _bound_in_scope(function: ast.AST | None, tree: ast.AST, name: str) -> str:
    """How `name` is bound where this call sits: "import", "local" or "".

    The reason this exists: `mock.patch.object(urlrequest, "urlopen") as urlopen`
    binds the *mock* to the name `urlopen`, and a test that then calls
    `urlopen()` is calling the mock. Reading that as the library would report the
    cleanest way to test a transport as a violation -- a rule that reports
    working code is the rule someone deletes.
    """
    imported = False
    for node in _own_scope(tree) if function is None else ast.walk(function):
        if isinstance(node, ast.Import):
            imported = imported or any(
                (alias.asname or alias.name.split(".")[0]) == name for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            imported = imported or any((alias.asname or alias.name) == name for alias in node.names)
        elif isinstance(node, ast.With):
            # `as urlopen` is the strongest statement available: after it, the
            # name *is* the mock, whatever an import at the top of the file said.
            if any(
                isinstance(item.optional_vars, ast.Name) and item.optional_vars.id == name
                for item in node.items
            ):
                return "local"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(argument.arg == name for argument in node.args.args + node.args.kwonlyargs):
                return "local"
        elif isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                return "local"
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return "local"
    return "import" if imported else ""


def _marked(lines: list[str], lineno: int) -> bool:
    """The marker on the line itself or the one above. An empty reason is nothing."""
    for candidate in (lineno, lineno - 1):
        if not 1 <= candidate <= len(lines):
            continue
        line = lines[candidate - 1]
        if MARKER in line and line.split(MARKER, 1)[1].strip():
            return True
    return False


def violations(files: dict[str, str]) -> list[str]:
    """Every unmarked way out of the process, as a reported string."""
    found: list[str] = []
    for name, source in sorted(files.items()):
        tree = ast.parse(source)
        lines = _source_lines(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called in NETWORK_CLIENTS:
                supplied = "transport" in {keyword.arg for keyword in node.keywords} or len(node.args) >= 2
                if supplied or _marked(lines, node.lineno):
                    continue
                found.append(
                    f"{name}:{node.lineno}: {called}(...) without a transport "
                    "(T-19: it falls back to the real one)"
                )
                continue
            if called == "urlopen":
                function = _enclosing_function(tree, node)
                if isinstance(func, ast.Name):
                    # A bare name is only the library when nothing bound it:
                    # `as urlopen` hands the test the mock, and calling it is
                    # the clean way to drive a transport, not a way out.
                    if _bound_in_scope(function, tree, called) == "local":
                        continue
                if function is not None and _patches_urlopen(function):
                    continue
                if _marked(lines, node.lineno):
                    continue
                where = function.name if function is not None else "<module>"
                found.append(
                    f"{name}:{node.lineno}: urlopen in {where}() with no patch and no marker "
                    "(T-19: it would use the real network)"
                )
    return found


def marked_counts(files: dict[str, str]) -> dict[str, int]:
    """How many usable markers each file carries (see `MARKER_SOURCE`)."""
    counted: dict[str, int] = {}
    for name, source in sorted(files.items()):
        if name == MARKER_SOURCE:
            continue
        number = sum(
            1
            for line in _source_lines(source)
            if MARKER in line and line.split(MARKER, 1)[1].strip()
        )
        if number:
            counted[name] = number
    return counted


def counts(files: dict[str, str]) -> dict[str, int]:
    """The denominator, so "found nothing" can be told from "looked at nothing"."""
    constructions = 0
    with_transport = 0
    urlopens = 0
    for source in files.values():
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called in NETWORK_CLIENTS:
                constructions += 1
                if "transport" in {keyword.arg for keyword in node.keywords} or len(node.args) >= 2:
                    with_transport += 1
            elif called == "urlopen":
                urlopens += 1
    return {
        "modules": len(files),
        "constructions": constructions,
        "with_transport": with_transport,
        "urlopen_calls": urlopens,
    }


class NoExternalServicesTests(unittest.TestCase):
    """The real test tree."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.files = test_files()

    def test_the_scan_sees_the_test_tree(self):
        """Denominator first: `violations` returning `[]` proves nothing alone.

        Measured at T-19: 40 modules, 38 client constructions (36 of them with an
        injected transport), 3 loopback `urlopen` calls.
        """
        measured = counts(self.files)
        self.assertGreaterEqual(measured["modules"], KNOWN_MODULES, measured)
        self.assertGreaterEqual(measured["constructions"], KNOWN_CONSTRUCTIONS, measured)
        self.assertGreaterEqual(measured["urlopen_calls"], 3, measured)

    def test_no_test_reaches_the_network(self):
        self.assertEqual(violations(self.files), [])

    def test_the_marked_sites_are_exactly_the_frozen_ones(self):
        """A ratchet in both directions: see `NETWORK_OK_COUNTS`."""
        self.assertEqual(marked_counts(self.files), NETWORK_OK_COUNTS)


class NoExternalServicesControls(unittest.TestCase):
    """Each half is shown reporting a planted violation.

    Without these, `test_no_test_reaches_the_network` would also pass if
    `violations` returned `[]` unconditionally.

    Every assertion reads only the planted file. Reading the whole tree's
    report instead makes a control fail because of an unrelated change in some
    other real test file -- which looks identical to the control working, and
    says nothing about the thing it names. The mutation table found that: with
    the whole-tree reading, deleting a marker in `test_bootstrap.py` turned four
    controls red for a reason none of them was written to detect.
    """

    PLANTED = "test_noop_executor.py"

    @classmethod
    def setUpClass(cls) -> None:
        cls.files = test_files()

    def plant(self, addition: str) -> list[str]:
        """The violations of the whole tree *against the planted file*."""
        files = dict(self.files)
        files[self.PLANTED] = files[self.PLANTED] + addition
        return [line for line in violations(files) if line.startswith(f"{self.PLANTED}:")]

    def test_control_for_a_client_without_a_transport(self):
        reported = self.plant('\n\nCLIENT = CloudAgentClient("http://cloud.test")\n')
        self.assertTrue(any("without a transport" in line for line in reported), reported)

    def test_control_for_an_unpatched_urlopen(self):
        reported = self.plant(
            '\n\ndef fetch():\n    with urlrequest.urlopen("https://example.com/") as response:\n'
            "        return response.read()\n"
        )
        self.assertTrue(any("no patch and no marker" in line for line in reported), reported)

    def test_control_for_a_marker_with_no_reason(self):
        """`# network-ok:` with nothing after it is still a violation.

        Otherwise the escape would be a keystroke, and the reason -- the only
        part a reader can use -- would be optional.
        """
        reported = self.plant('\n\nCLIENT = CloudAgentClient("http://cloud.test")  # network-ok:\n')
        self.assertTrue(any("without a transport" in line for line in reported), reported)

    def test_control_for_a_patched_urlopen(self):
        """The rule is not "any urlopen is a violation".

        `test_cloud_agent_client.py` legitimately builds the real transport and
        patches `urlopen`; a rule that reported it would be reporting the
        cleanest way to test a transport.
        """
        reported = self.plant(
            '\n\ndef fetch():\n    with mock.patch.object(urlrequest, "urlopen") as urlopen:\n'
            "        urlopen.return_value.read.return_value = b'{}'\n        return urlopen()\n"
        )
        self.assertEqual(reported, [])

    def test_control_for_an_imported_urlopen(self):
        """The paired control for the one above: "bound" must not mean "imported".

        `from urllib.request import urlopen` is exactly the spelling that reaches
        the network, so the exemption has to be narrow enough to leave it alone.
        """
        reported = self.plant(
            "\n\nfrom urllib.request import urlopen\n\nurlopen('https://example.com/')\n"
        )
        self.assertTrue(any("no patch and no marker" in line for line in reported), reported)

    def test_control_for_a_rule_that_stops_looking(self):
        """The reported list is what the assertions above read.

        A `violations` that returned `[]` would make every arm above pass by
        having nothing to say; this pins that it reports when something is
        planted -- and that it reports *twice*, so a rule that only ever emits
        one shape is caught by the count rather than by the two arms agreeing.
        """
        reported = self.plant(
            '\n\nCLIENT = BitBrowserClient("http://127.0.0.1:54345")\n'
            '\n\ndef fetch():\n    with urlrequest.urlopen("https://example.com/") as response:\n'
            "        return response.read()\n"
        )
        self.assertEqual(len(reported), 2, reported)


class MarkerRatchetControls(unittest.TestCase):
    """`marked_counts` compared by equality fails in both directions.

    The ratchet is the only thing keeping the escape hatch honest, so it gets
    the same treatment as the rule: shown reporting a change it should report.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.files = test_files()

    def test_control_for_a_marker_that_disappeared(self):
        files = dict(self.files)
        files["test_local_health.py"] = files["test_local_health.py"].replace(MARKER, "# (marker gone)")
        self.assertNotEqual(marked_counts(files), NETWORK_OK_COUNTS)

    def test_control_for_a_marker_nobody_declared(self):
        files = dict(self.files)
        files["test_noop_executor.py"] = files["test_noop_executor.py"] + (
            "\n# network-ok: planted, and this file is not in the frozen table\n"
        )
        self.assertNotEqual(marked_counts(files), NETWORK_OK_COUNTS)


if __name__ == "__main__":
    unittest.main()
