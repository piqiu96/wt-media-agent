"""Patch targets must stay resolvable (CHG-056 T-05).

A `mock.patch("a.b.c")` string is a dependency on the *shape* of the source that
no type checker sees: if `a/b.py` stops binding `c`, or starts reaching through
`import a.b as c` instead, the string keeps looking fine and the test fails at
run time with an AttributeError that reads like a test bug.

The rule this file pins, and the reason the source side spells names the way it
does:

    **every name the tests patch is bound in the patched module's own
    namespace** -- defined there, or brought in by an import statement. A module
    that reaches through an alias instead (`from X import net` then
    `net.proxy.call_it()`) binds `net`, not `call_it`, and the patch string dies
    with an AttributeError at run time.

The machine check is "is the name bound", which is deliberately the weaker of
the two facts: `import X as name` also binds `name`, and this test cannot tell
it from `from X import name`. That spelling cannot be called, so it does not
occur -- and the bound-name check is what the failure actually depends on.

Three of those names are load-bearing beyond their own test, so they are listed
explicitly below: they are the seams where the local API's I/O is replaced.

What this file cannot check: `patch.object`/`patch.dict`, which take an object
or a mapping rather than a dotted name (a `patch.dict("a.b")` string names a
dict to update, not a callable, so it is not resolved here); targets built at
run time from strings; and whether a patched name is *used through the patched
binding* rather than a local alias captured earlier.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PACKAGE = "wt_media_agent"
TESTS = ROOT / "tests"

#: Patch targets whose names are frozen by other artefacts or by the packaging
#: step, with why each one cannot simply be renamed.
FROZEN_PATCH_TARGETS = {
    f"{PACKAGE}.local_api.server.urlrequest.urlopen":
        "the local API's only outbound HTTP call; tests/test_local_account_check.py "
        "and tests/test_proxy_extract.py replace it at this path",
    f"{PACKAGE}.local_api.server.check_proxy_connectivity":
        "tests/test_proxy_check.py replaces it at this path, which is why "
        "local_api/server.py must keep `from ...proxy import check_proxy_connectivity`",
    f"{PACKAGE}.sidecar_main.local_api_server":
        "the name the frozen entry module keeps for the local API surface "
        "(ADR-0016 §2: the entry may only spell out names that exist)",
}

#: Floor for the discovered set, so a discovery that stops walking test files
#: fails here rather than passing as "no targets to resolve".
KNOWN_SITES = 5
KNOWN_TEST_FILES = 30


def test_files() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(TESTS.glob("test_*.py"))}


def patch_targets(source: str) -> list[tuple[int, str]]:
    """Every literal string argument of a `patch(...)` call, with its line."""
    targets = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if called not in ("patch", "patch.object"):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            targets.append((node.lineno, first.value))
    return targets


def resolve(target: str) -> object:
    """Import the longest module prefix, then follow the attributes.

    Raises `LookupError` when no prefix is importable and `AttributeError` when
    an attribute is missing -- both are failures this test reports verbatim.
    """
    parts = target.split(".")
    for cut in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:cut]))
        except ImportError:
            continue
        for attr in parts[cut:]:
            obj = getattr(obj, attr)
        return obj
    raise LookupError(f"no importable module prefix in {target!r}")


def bound_names(source: str) -> set[str]:
    """Module-level names the module binds: definitions, assignments, imports."""
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def binding_module(target: str) -> tuple[str, str]:
    """`(module, first attribute patched)` -- the module whose namespace matters."""
    parts = target.split(".")
    for cut in range(len(parts), 0, -1):
        name = ".".join(parts[:cut])
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        rest = parts[cut:]
        if rest:
            return name, rest[0]
        return name, ""
    raise LookupError(f"no importable module prefix in {target!r}")


class PatchTargetDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.files = test_files()
        cls.sites = [(name, line, target) for name, source in cls.files.items() for line, target in patch_targets(source)]

    def test_the_discovery_finds_the_targets_that_exist(self):
        """Denominator first: `patch_targets` returning nothing proves nothing."""
        self.assertGreaterEqual(len(self.files), KNOWN_TEST_FILES)
        self.assertGreaterEqual(len(self.sites), KNOWN_SITES)
        found = {target for _, _, target in self.sites}
        self.assertTrue(
            {f"{PACKAGE}.local_api.server.urlrequest.urlopen",
             f"{PACKAGE}.local_api.server.check_proxy_connectivity"} <= found,
            found,
        )

    def test_every_patch_target_resolves(self):
        for name, line, target in self.sites:
            with self.subTest(target=target):
                try:
                    resolve(target)
                except (LookupError, AttributeError) as error:
                    self.fail(f"{name}:{line} patches {target!r}, which does not resolve: {error}")

    def test_the_frozen_patch_targets_still_resolve(self):
        for target, reason in sorted(FROZEN_PATCH_TARGETS.items()):
            with self.subTest(target=target):
                try:
                    resolve(target)
                except (LookupError, AttributeError) as error:
                    self.fail(f"{target} no longer resolves ({reason}): {error}")

    def test_every_patched_name_is_bound_in_the_module_it_is_patched_on(self):
        """The rule the source side has to keep (see the module docstring)."""
        for name, line, target in self.sites:
            if not target.startswith(PACKAGE + "."):
                continue
            with self.subTest(target=target):
                module_name, attr = binding_module(target)
                if not attr:
                    continue
                source = Path(importlib.import_module(module_name).__file__).read_text(encoding="utf-8")
                self.assertIn(
                    attr,
                    bound_names(source),
                    f"{name}:{line} patches {target!r}, but {module_name} does not bind "
                    f"{attr!r} -- the source must use `from X import {attr}`",
                )

    def test_a_target_that_does_not_resolve_is_reported(self):
        """Control: the resolver fails when it should."""
        with self.assertRaises(AttributeError):
            resolve(f"{PACKAGE}.local_api.server.no_such_function_exists")
        with self.assertRaises(LookupError):
            resolve("no_such_package_at_all.thing")

    def test_the_binding_rule_rejects_reaching_through_a_module_alias(self):
        """Control: the refactor that breaks the target is `from X import mod`.

        Called as `net.proxy.check_proxy_connectivity(...)`, the module binds
        `net` and the patch string has nothing to attach to -- so the rule must
        report the name as unbound, and the `from X import name` form as bound.
        """
        through_module = "from wt_media_agent.services import net\n"
        self.assertNotIn("check_proxy_connectivity", bound_names(through_module))
        self.assertIn("net", bound_names(through_module))

        direct = "from wt_media_agent.services.net.proxy import check_proxy_connectivity\n"
        self.assertIn("check_proxy_connectivity", bound_names(direct))


if __name__ == "__main__":
    unittest.main()
