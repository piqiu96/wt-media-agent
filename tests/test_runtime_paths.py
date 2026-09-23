import tempfile
import unittest
from pathlib import Path

from wt_media_agent.runtime import paths as runtime_paths
from wt_media_agent.runtime.paths import RuntimePaths


class RuntimePathsResolveTest(unittest.TestCase):
    def test_override_wins_over_everything(self):
        rp = RuntimePaths.resolve(
            data_dir="/tmp/wt-override",
            environment="production",
            frozen=True,
            home=Path("/home/nobody"),
        )
        self.assertEqual(rp.origin, "override")
        self.assertEqual(rp.data_dir, Path("/tmp/wt-override"))
        self.assertEqual(rp.logs_dir, Path("/tmp/wt-override/logs"))
        self.assertEqual(rp.versions_dir, Path("/tmp/wt-override/versions"))

    def test_override_expands_user(self):
        rp = RuntimePaths.resolve(data_dir="~/wt-agent")
        self.assertTrue(rp.data_dir.is_absolute())
        self.assertNotIn("~", str(rp.data_dir))

    def test_blank_override_is_treated_as_unset(self):
        for blank in ("", "   ", "\t"):
            rp = RuntimePaths.resolve(
                data_dir=blank, environment="development", frozen=False,
                repo_root=Path("/repo"),
            )
            self.assertEqual(rp.origin, "dev", f"data_dir={blank!r} should not be an override")

    def test_dev_state_uses_repo_local(self):
        rp = RuntimePaths.resolve(environment="development", frozen=False, repo_root=Path("/repo"))
        self.assertEqual(rp.origin, "dev")
        self.assertEqual(rp.data_dir, Path("/repo/.local/data"))
        self.assertEqual(rp.logs_dir, Path("/repo/.local/logs"))
        self.assertEqual(rp.versions_dir, Path("/repo/.local/data/versions"))

    def test_production_state_uses_installed_locations(self):
        rp = RuntimePaths.resolve(environment="production", frozen=False, home=Path("/Users/dev"))
        self.assertEqual(rp.origin, "installed")
        self.assertEqual(
            rp.data_dir, Path("/Users/dev/Library/Application Support/WTMedia/Agent")
        )
        self.assertEqual(rp.logs_dir, Path("/Users/dev/Library/Logs/WTMedia/Agent"))
        self.assertEqual(rp.versions_dir, rp.data_dir / "versions")

    def test_frozen_state_uses_installed_locations_even_in_development(self):
        rp = RuntimePaths.resolve(environment="development", frozen=True, home=Path("/Users/dev"))
        self.assertEqual(rp.origin, "installed")
        self.assertEqual(
            rp.data_dir, Path("/Users/dev/Library/Application Support/WTMedia/Agent")
        )

    def test_environment_matching_ignores_case_and_padding(self):
        for value in ("Production", " production ", "PRODUCTION"):
            rp = RuntimePaths.resolve(environment=value, frozen=False, home=Path("/Users/dev"))
            self.assertEqual(rp.origin, "installed", f"{value!r} should count as production")

    def test_is_frozen_reads_no_environment_variable(self):
        """R6 depends on this: only runtime/config.py may read the environment."""
        source = Path(runtime_paths.__file__).read_text()
        self.assertNotIn("os.environ", source)
        self.assertNotIn("getenv", source)
        self.assertNotIn("import os", source)


class DefaultLogFileTest(unittest.TestCase):
    def test_dev_and_override_leave_logs_on_stderr(self):
        for kwargs in (
            {"environment": "development", "frozen": False, "repo_root": Path("/repo")},
            {"data_dir": "/tmp/wt-override"},
        ):
            self.assertEqual(RuntimePaths.resolve(**kwargs).default_log_file, "")

    def test_installed_writes_a_real_file(self):
        rp = RuntimePaths.resolve(environment="production", frozen=False, home=Path("/Users/dev"))
        self.assertEqual(rp.default_log_file, "/Users/dev/Library/Logs/WTMedia/Agent/agent.log")


class EnsureTest(unittest.TestCase):
    def test_creates_all_three_then_reports_nothing_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            # tmp itself already exists; the override target must not, or the
            # first call would legitimately create only two directories.
            rp = RuntimePaths.resolve(data_dir=str(Path(tmp) / "agent"))
            created = rp.ensure()
            self.assertEqual(len(created), 3)
            for directory in (rp.data_dir, rp.logs_dir, rp.versions_dir):
                self.assertTrue(directory.is_dir(), f"{directory} missing")
            # Second call is a no-op, which is what makes `ensure()` safe to
            # call on every start.
            self.assertEqual(rp.ensure(), ())

    def test_a_failing_tree_does_not_stop_a_separate_one(self):
        """Independence is only observable in installed mode.

        Installed mode puts data under `Application Support` and logs under
        `Library/Logs` -- two separate trees. Blocking the logs tree must not
        prevent the data tree from being created.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "Library").mkdir()
            (home / "Library" / "Logs").write_text("a file where a directory must go")
            rp = RuntimePaths.resolve(environment="production", frozen=False, home=home)

            created = rp.ensure()  # must not raise

            self.assertTrue(rp.data_dir.is_dir(), "data tree should still be created")
            self.assertTrue(rp.versions_dir.is_dir())
            self.assertFalse(rp.logs_dir.is_dir(), "logs tree is blocked by the file")
            self.assertIn(rp.data_dir, created)
            self.assertNotIn(rp.logs_dir, created)

    def test_every_directory_failing_still_does_not_raise(self):
        """Override mode nests all three, so one bad parent blocks all three.

        The contract under test is only that `ensure()` returns instead of
        propagating: the Agent must start and report, not crash.
        """
        with tempfile.TemporaryDirectory() as tmp:
            blocked = Path(tmp) / "data"
            blocked.write_text("i am a file, so mkdir must fail")
            rp = RuntimePaths.resolve(data_dir=str(blocked))
            created = rp.ensure()  # must not raise
            self.assertEqual(created, ())
            self.assertFalse(rp.data_dir.is_dir())
            self.assertFalse(rp.logs_dir.is_dir())
            self.assertFalse(rp.versions_dir.is_dir())


if __name__ == "__main__":
    unittest.main()
