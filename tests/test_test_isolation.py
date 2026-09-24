"""Isolation of the test suite itself, not of the Agent.

CHG-057 D-09 (the user's ruling) and architecture baseline §5.13 both require the
tests to use `tmpdir/data`, `tmpdir/logs`, `tmpdir/runtime`, and forbid
`REPO_ROOT/.local/data`. This module holds the two halves of that requirement:
the shared helper tests use to get there, and a rule that fails the suite if a
test starts treating the checkout as a runtime directory again.
"""

import re
import unittest
from pathlib import Path

from support import isolated_paths
from wt_media_agent.runtime.config import get_config, reset_config_cache
from wt_media_agent.runtime.paths import repository_root

TESTS_DIR = Path(__file__).resolve().parent


class IsolatedPathsTest(unittest.TestCase):
    def test_it_creates_the_three_directories(self):
        with isolated_paths() as paths:
            for directory in (paths.data, paths.logs, paths.runtime):
                self.assertTrue(directory.is_dir(), f"{directory} missing")

    def test_the_config_resolves_inside_the_temp_tree_and_never_the_checkout(self):
        with isolated_paths() as paths:
            cfg = get_config()

            self.assertEqual(cfg.paths.origin, "override")
            self.assertEqual(cfg.paths.data_dir, paths.data)
            self.assertEqual(cfg.paths.logs_dir, paths.logs)
            checkout = repository_root()
            for directory in (cfg.paths.data_dir, cfg.paths.logs_dir, cfg.paths.versions_dir):
                self.assertFalse(
                    directory.is_relative_to(checkout),
                    f"{directory} is inside the checkout {checkout}",
                )

    def test_it_restores_the_environment_and_the_cache(self):
        before = get_config()
        with isolated_paths():
            self.assertNotEqual(get_config(), before)
        self.assertEqual(get_config(), before)


class NoTestTreatsTheCheckoutAsARuntimeDirectoryTest(unittest.TestCase):
    """The checkout is readable, not writable.

    Reading source files from a `__file__`-derived root is normal here -- the
    boundary and patch-target tests do it deliberately. What this rule forbids is
    *joining* such a root with `.local`, the tree the Agent writes its database
    and (since T-03) its logs into. An assertion built that way resolves to the
    live checkout, so the first test that then called `ensure()` would write into
    the developer's own tree -- and, on a clean clone, create it.
    """

    #: `<identifier> / ".local"` -- a derived root joined with the runtime tree.
    JOINED_LOCAL = re.compile(r"""\b[A-Za-z_]\w*\s*/\s*["']\.local["']""")

    #: This module is the one place that must be able to name the shape it bans.
    EXEMPT = {"test_test_isolation.py"}

    def scanned(self) -> list[Path]:
        modules = sorted(
            path for path in TESTS_DIR.glob("test_*.py") if path.name not in self.EXEMPT
        )
        self.assertGreater(len(modules), 0, "no test modules matched -- the scan is vacuous")
        return modules

    def test_no_test_module_joins_a_derived_root_with_local(self):
        modules = self.scanned()
        offenders = []
        for path in modules:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if self.JOINED_LOCAL.search(line):
                    offenders.append(f"{path.name}:{number}: {line.strip()}")

        self.assertEqual(
            offenders,
            [],
            "these tests resolve into the checkout's .local/ instead of a tmpdir "
            "(CHG-057 D-09 / §5.13):\n  " + "\n  ".join(offenders),
        )

    def test_the_rule_can_flag_the_shape_it_forbids(self):
        """Positive control: a rule that cannot fail proves nothing.

        Without this, deleting the regex would leave the test above passing over
        an empty match set and nobody would notice.
        """
        for sample in (
            'x = REPO_ROOT / ".local" / "data"',
            "y = ROOT /  '.local'",
        ):
            with self.subTest(sample=sample):
                self.assertTrue(self.JOINED_LOCAL.search(sample), sample)

    def test_the_rule_does_not_flag_an_injected_root(self):
        """`Path("/repo/.local/data")` is a *fake* root, and must stay allowed.

        It is the form `test_runtime_paths.py` uses to assert the dev rule without
        touching this machine's checkout -- flagging it would push tests back
        toward the live tree, which is the opposite of the point.
        """
        for sample in (
            'self.assertEqual(rp.data_dir, Path("/repo/.local/data"))',
            'self.assertEqual(rp.logs_dir, Path("/repo/.local/logs"))',
        ):
            with self.subTest(sample=sample):
                self.assertIsNone(self.JOINED_LOCAL.search(sample), sample)


if __name__ == "__main__":
    unittest.main()
