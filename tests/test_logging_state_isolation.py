"""A test module that assembles the Agent must put global logging state back.

`build_components` is the single Logger entry point (CHG-057, the user's ruling
二/五). That is the requirement; the consequence for this suite is that any test
reaching the bootstrap surface installs three file handlers on the root *and*
package loggers, aimed at that test's throwaway directory. Restoring them is
`LoggingStateTestCase`'s job, and this check is what makes it a rule instead of
a habit.

It is here because the habit failed, measured: two modules skipped the restore
and every test after them inherited their dead handler set, so six later tests
printed `--- Logging error --- FileNotFoundError` (CHG-057 T-06). Nothing failed;
the suite simply stopped being trustworthy to read.

The rule is keyed on reaching `wt_media_agent.bootstrap`, which is what both
offenders share and what makes them offenders: the assembly surface is where the
Logger is initialized, whether it is called directly (`build_components`) or
through an entry (`bootstrap.sidecar.run`).
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

TESTS = Path(__file__).resolve().parent

#: The package whose import means "this module reaches the assembly".
ASSEMBLY_IMPORT = "wt_media_agent.bootstrap"

#: How many modules reach it today. Used as the scan's denominator and as its
#: positive control: a scan that matched nothing would pass without checking
#: anything at all.
MIN_MATCHING_MODULES = 2


def modules_reaching_the_assembly() -> list[tuple[Path, str]]:
    """Every test module that imports the assembly surface, except this one.

    This file names the import in order to look for it, and assembles nothing.
    Excluding it by path rather than by a cleverer pattern keeps the rule
    readable; a scan that exempts itself quietly would be worse.
    """
    this_file = Path(__file__).resolve()
    return [
        (path, path.read_text())
        for path in sorted(TESTS.glob("test_*.py"))
        if path.resolve() != this_file and ASSEMBLY_IMPORT in path.read_text()
    ]


def base_class_names(source: str) -> set[str]:
    """Every name used as a base class anywhere in the module.

    Parsed rather than grepped: a module may satisfy the rule with a local base
    that other classes then inherit (`ComponentTestCase` in `test_bootstrap.py`
    is exactly that), so the check is "some class in this file inherits it",
    not "every class spells it out".
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                if isinstance(base, ast.Name):
                    names.add(base.id)
                elif isinstance(base, ast.Attribute):
                    names.add(base.attr)
    return names


class AssemblyModulesRestoreLoggingTest(unittest.TestCase):
    def test_a_module_that_reaches_the_assembly_restores_logging_state(self):
        matched = modules_reaching_the_assembly()
        self.assertGreaterEqual(
            len(matched),
            MIN_MATCHING_MODULES,
            f"the scan matched {len(matched)} module(s); it cannot be trusted to fail",
        )

        for path, source in matched:
            with self.subTest(module=path.name):
                self.assertIn(
                    "LoggingStateTestCase",
                    base_class_names(source),
                    f"{path.name} assembles the Agent but never restores logging state",
                )


if __name__ == "__main__":
    unittest.main()
