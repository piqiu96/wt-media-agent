"""Arms for `bin/control.sh`'s *entry* properties -- not for its start/stop
behaviour (that needs the venv, a port and a real health server).

Why this file exists: the entry is documented as a direct invocation
(`bin/control.sh status`), and a mode that is executable on one machine can
still be 100644 in the git index -- which is what a fresh checkout and CI
materialise. Only the index can tell us that, and only a *direct* invocation can
tell us whether the file runs at all: `bash bin/control.sh ...` succeeds even
when the execute bit is missing, so a check written that way cannot see the very
defect it is meant to catch. Every arm below therefore invokes the entry
DIRECTLY, through `subprocess` with the path as argv[0].

Two details this file deliberately does not do:

* It does not assert a literal exit code for a non-executable entry. That case
  reads differently depending on the caller's shell options -- measured: no
  options / `-u` / `-o pipefail` -> 126, `-e` / `-eu` -> 1 -- and Python's
  `subprocess` raises `PermissionError` before any code exists at all. The arms
  assert the property (exit 0) instead, and report the `OSError` verbatim.
* It does not skip when git is missing. The index mode is the half of this
  property that a working-tree check cannot see; a silent skip would leave the
  suite green while checking half of what it claims to.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
CONTROL = ROOT_DIR / "bin" / "control.sh"
VERBS = ("start", "stop", "restart", "status")


class EntryNotInvocable(Exception):
    """The entry could not be executed at all -- the defect this file checks for."""


def run_control(*args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the entry directly. Raises `EntryNotInvocable` when the OS refuses."""
    try:
        return subprocess.run(
            [str(CONTROL), *args],
            capture_output=True,
            text=True,
        )
    except OSError as exc:  # EACCES on a non-executable entry lands here
        raise EntryNotInvocable(exc) from exc


class ControlEntryTest(unittest.TestCase):
    def assert_invocable(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return run_control(*args)
        except EntryNotInvocable as exc:
            self.fail(
                "bin/control.sh could not be invoked directly "
                f"({' '.join(args)}): {exc}. A non-executable entry is the point "
                "of this file -- run `chmod +x bin/control.sh`."
            )

    def test_entry_exists(self) -> None:
        self.assertTrue(CONTROL.is_file(), f"not a regular file: {CONTROL}")

    def test_entry_is_tracked_by_git(self) -> None:
        git = shutil.which("git")
        self.assertIsNotNone(git, "git is required to read the index mode")
        result = subprocess.run(
            [str(git), "-C", str(ROOT_DIR), "ls-files", "--error-unmatch", "bin/control.sh"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr.strip())

    def test_index_mode_is_100755(self) -> None:
        git = shutil.which("git")
        self.assertIsNotNone(git, "git is required to read the index mode")
        result = subprocess.run(
            [str(git), "-C", str(ROOT_DIR), "ls-files", "-s", "--", "bin/control.sh"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr.strip())
        mode = result.stdout.split()[0]
        self.assertEqual(
            mode,
            "100755",
            f"git index mode is {mode}; a fresh checkout would not be executable",
        )

    def test_working_tree_mode_is_executable(self) -> None:
        self.assertTrue(os.access(CONTROL, os.X_OK), f"not executable: {CONTROL}")

    def test_direct_invocation_help_exits_zero(self) -> None:
        result = self.assert_invocable("help")
        self.assertEqual(
            result.returncode,
            0,
            f"exit={result.returncode}; stderr: {result.stderr.strip()[:200]}",
        )

    def test_help_lists_every_verb(self) -> None:
        stdout = self.assert_invocable("help").stdout
        for verb in VERBS:
            # Anchored at the start of a line, followed by a word boundary: a
            # bare `verb in stdout` would be satisfied by the `restart` line
            # alone, so a help that had lost `start` would still look complete.
            self.assertRegex(
                stdout,
                re.compile(rf"^  {verb}\b", re.MULTILINE),
                f"help does not list '{verb}': {stdout!r}",
            )

    def test_unknown_verb_exits_two_with_usage_on_stderr(self) -> None:
        result = self.assert_invocable("no-such-verb")
        self.assertEqual(result.returncode, 2, f"exit={result.returncode}")
        self.assertEqual(result.stdout, "", "unknown verb must write nothing to stdout")
        self.assertIn("Usage:", result.stderr)


if __name__ == "__main__":
    unittest.main()
