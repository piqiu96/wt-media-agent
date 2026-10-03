"""T-05/T-10: the release configuration reaches the product wholesale, and unread.

Three claims about one story, so one file. `config_online/` is copied into the
product as a whole directory (`sync_config`, ADR-0016 §6) and has to arrive
without anything the runtime would refuse to honour (`SENSITIVE_KEY_NAMES`,
ADR-0016 §7) and without a comment that states a decision as still pending
(`UNRESOLVED_MARKERS`, T-10). All three are about shipped bytes rather than
about a running Agent, which is why they are not in `test_runtime_config.py` --
that file's subject is what a process resolves, this one's is what a release
ships.
"""

from __future__ import annotations

import importlib.util
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from wt_media_agent.runtime.config import SENSITIVE_KEY_NAMES

ROOT = Path(__file__).resolve().parents[1]
ONLINE_DIR = ROOT / "config_online"


def load_script():
    """`scripts/build_desktop_sidecar.py` by path -- it is not an importable package."""
    spec = importlib.util.spec_from_file_location(
        "build_desktop_sidecar", ROOT / "scripts" / "build_desktop_sidecar.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _leaves(node: dict, prefix: str = "") -> list[tuple[str, str, object]]:
    """`(dotted path, leaf name, value)` for every scalar, values included.

    The loader's own `_leaves` stops at the name, because it only ever needs to
    decide whether to ignore a key. A shipping scan needs the value too: a
    credential pasted into an ordinary key's value is a leak the name check
    cannot see.
    """
    found: list[tuple[str, str, object]] = []
    for key, value in node.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            found.extend(_leaves(value, f"{path}."))
        else:
            found.append((path, key, value))
    return found


def _carries_userinfo(value: object) -> bool:
    """`scheme://user:password@host/...` -- the shape a credential leak takes."""
    if not isinstance(value, str):
        return False
    _, separator, rest = value.partition("://")
    if not separator:
        return False
    return "@" in rest.split("/", 1)[0]


def credential_findings(directory: Path) -> tuple[list[str], int]:
    """Credentials a release must not carry, plus how many leaves were looked at.

    The denominator is returned so a caller can tell "nothing was found" from
    "nothing was looked at" -- an empty scan and a clean scan read the same way
    otherwise.
    """
    findings: list[str] = []
    scanned = 0
    for path in sorted(directory.rglob("*.toml")):
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        for dotted, leaf, value in _leaves(document):
            scanned += 1
            if leaf.lower() in SENSITIVE_KEY_NAMES:
                findings.append(f"{path.name}: {dotted} is a sensitive key name")
            if _carries_userinfo(value):
                findings.append(f"{path.name}: {dotted} carries credentials in its value")
    return findings, scanned


#: How a shipped file states that a decision is still pending. Phrasing, not
#: question ids: naming `Q-01` is fine once the question is answered -- what a
#: release must not ship is a claim that it is not.
UNRESOLVED_MARKERS = (
    "still open",
    "undecided",
    "unresolved",
    "resolve before release",
)


def unresolved_claims(directory: Path) -> tuple[list[str], int]:
    """Pending-decision claims a release must not carry, plus files looked at.

    Every file in the directory is scanned, not only the `*.toml` ones: the
    claim that the production Cloud address was undecided lived in a README
    beside the config as well, and a sentence is not less shipped for being in
    prose.
    """
    findings: list[str] = []
    scanned = 0
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        scanned += 1
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            lowered = line.lower()
            for marker in UNRESOLVED_MARKERS:
                if marker in lowered:
                    findings.append(f"{path.name}:{number} states '{marker}'")
    return findings, scanned


class ConfigSyncTest(unittest.TestCase):
    """`config_online/` becomes the product's `config/` wholesale."""

    def setUp(self):
        self.script = load_script()

    def test_the_product_configuration_is_a_wholesale_mirror(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "config"
            copied = self.script.sync_config(ROOT, destination)
            self.assertEqual(self.script.mirror_differences(ONLINE_DIR, destination), [])
            # Denominator: two empty directories are also "no differences".
            self.assertIn("agent.toml", copied)
            self.assertGreaterEqual(len(copied), 2)
            self.assertEqual(
                sorted(p.name for p in destination.iterdir()),
                sorted(p.name for p in ONLINE_DIR.iterdir()),
            )

    def test_a_copy_over_an_existing_directory_drops_what_the_source_lost(self):
        """Wholesale, not a merge: this is the arm a merge-style copy fails.

        The stale files are exactly the two shapes drift takes -- a file deleted
        from `config_online/` and a value edited back in the product.
        """
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "config"
            destination.mkdir()
            (destination / "agent.toml").write_text('environment = "development"\n')
            (destination / "left-behind.toml").write_text('token = "stale"\n')

            copied = self.script.sync_config(ROOT, destination)

            self.assertNotIn("left-behind.toml", copied)
            self.assertFalse((destination / "left-behind.toml").exists())
            self.assertEqual(
                (destination / "agent.toml").read_bytes(),
                (ONLINE_DIR / "agent.toml").read_bytes(),
            )
            self.assertEqual(self.script.mirror_differences(ONLINE_DIR, destination), [])

    def test_syncing_from_a_source_that_is_not_there_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as caught:
                self.script.sync_config(Path(tmp), Path(tmp) / "config")
        self.assertIn("config_online", str(caught.exception))

    def test_a_copy_that_did_not_reproduce_the_source_is_refused(self):
        """The read-back is for a copy that only looks like it worked.

        `copytree` is replaced here rather than a directory being doctored
        afterwards: the claim under test is that the check runs *after* the copy
        and catches what the copy did, so the copy is what has to lie.
        """
        with tempfile.TemporaryDirectory() as tmp:
            real_copytree = shutil.copytree

            def lossy(source, destination, **kwargs):
                real_copytree(source, destination, **kwargs)
                (Path(destination) / "agent.toml").unlink()

            with mock.patch.object(self.script.shutil, "copytree", lossy):
                with self.assertRaises(RuntimeError) as caught:
                    self.script.sync_config(ROOT, Path(tmp) / "config")

        self.assertIn("missing agent.toml", str(caught.exception))

    def test_the_mirror_check_names_every_way_a_copy_can_disagree(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            destination = Path(tmp) / "destination"
            source.mkdir()
            destination.mkdir()
            (source / "agent.toml").write_text('environment = "production"\n')
            (source / "README.md").write_text("shipping notes\n")
            (destination / "agent.toml").write_text('environment = "production"\n')

            self.assertEqual(
                self.script.mirror_differences(source, destination), ["missing README.md"]
            )
            (destination / "README.md").write_text("shipping notes\n")
            (destination / "extra.toml").write_text("x = 1\n")
            self.assertEqual(
                self.script.mirror_differences(source, destination), ["unexpected extra.toml"]
            )
            (destination / "extra.toml").unlink()
            (destination / "agent.toml").write_text('environment = "development"\n')
            self.assertEqual(
                self.script.mirror_differences(source, destination), ["changed agent.toml"]
            )
            # Control: identical directories are reported as identical, so the
            # three arms above are differences being found rather than a check
            # that always returns something.
            (destination / "agent.toml").write_text('environment = "production"\n')
            self.assertEqual(self.script.mirror_differences(source, destination), [])


class ConfigSyncCliTest(unittest.TestCase):
    """The command line Desktop's release scripts call.

    `stage-release-config.sh` passes `--config-dir` for an app that Tauri has
    already bundled, so this mode must work without the freeze -- and must not
    quietly do nothing when it is handed no work at all.
    """

    def setUp(self):
        self.script = load_script()

    def test_staging_the_configuration_alone_does_not_freeze_a_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "Contents" / "Resources" / "config"
            with mock.patch.object(self.script.platform, "system", return_value="Linux"):
                self.assertEqual(self.script.main(["--config-dir", str(destination)]), 0)
            self.assertEqual(self.script.mirror_differences(ONLINE_DIR, destination), [])

    def test_a_call_with_nothing_to_do_is_an_error(self):
        with self.assertRaises(SystemExit) as caught:
            self.script.main([])
        self.assertEqual(caught.exception.code, 2, "argparse's usage error")

    def test_the_build_arguments_still_go_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as caught:
                self.script.main(["--output-dir", tmp])
        self.assertEqual(caught.exception.code, 2, "argparse's usage error")


class ShippedConfigurationTest(unittest.TestCase):
    """What `config_online/` may carry, given that it ships."""

    def test_the_shipped_configuration_carries_no_credential(self):
        findings, scanned = credential_findings(ONLINE_DIR)
        self.assertGreater(scanned, 0, "the scan must have looked at something")
        self.assertEqual(findings, [], "credentials are environment-only (ADR-0016 §7)")

    def test_the_credential_scan_finds_both_shapes_it_claims_to(self):
        """Positive control: the check above only means something if it can fail.

        Both shapes are planted, because the negative above is two claims -- no
        sensitive key name, no credential in a value -- and one control for two
        claims would leave the second unproven.

        The planted key is nested (`agent.Runtime_Token`, not `runtime_token`) and
        mixed-case on purpose: the rule being pinned is "the leaf name, matched
        the way the loader matches it" (`SENSITIVE_KEY_NAMES`, case-insensitive).
        A top-level lowercase key would also be found by a scan comparing full
        dotted paths case-sensitively, so it could not tell the two rules apart.
        """
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "clean.toml").write_text('[agent]\nid = "local-agent-dev"\n')
            (directory / "planted.toml").write_text(
                '[agent]\nRuntime_Token = "not-a-real-token"\n'
                '[cloud]\nbase_url = "http://user:hunter2@cloud.invalid"\n'
            )

            findings, scanned = credential_findings(directory)

            self.assertEqual(scanned, 3, "one scan per leaf: id, Runtime_Token, base_url")
            self.assertIn("planted.toml: agent.Runtime_Token is a sensitive key name", findings)
            self.assertIn("planted.toml: cloud.base_url carries credentials in its value", findings)
            self.assertEqual(len(findings), 2, "the clean file must contribute nothing")

    def test_the_shipped_configuration_claims_no_open_question(self):
        """A release must not ship "this was never decided" about its own values.

        The value is not the thing that rots here. `cloud.base_url` kept the
        loopback address through the whole of CHG-056, which is a defensible
        release value; what made the file wrong was the comment beside it
        promising a resolution later. That sentence shipped, and the file it
        shipped in was the one that said what production talks to.

        `Q-01` by name is not a finding: a decided question may be named. See
        `UNRESOLVED_MARKERS`.
        """
        findings, scanned = unresolved_claims(ONLINE_DIR)
        self.assertGreater(scanned, 0, "the scan must have looked at something")
        self.assertEqual(
            findings, [], "a shipped configuration states decisions as made, not as pending"
        )

    def test_the_pending_decision_scan_finds_its_markers(self):
        """Positive control, and the reason the markers are phrasing not ids.

        Both halves are planted: the same sentence in a `*.toml` and in a
        README, because the scan is a whole-directory one and a `*.toml`-only
        version would pass this control while missing one of the two files that
        were actually wrong. The third file pins the other direction -- naming
        an answered question must not be reported.
        """
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "README.md").write_text(
                "- `cloud.base_url` holds the loopback value -- the production address is undecided.\n"
            )
            (directory / "agent.toml").write_text(
                '# Q-01 is still open. Resolve before release.\nbase_url = "http://127.0.0.1:18080"\n'
            )
            (directory / "decided.toml").write_text(
                '# Decided: Q-01 is closed, the production address stays loopback.\n'
            )

            findings, scanned = unresolved_claims(directory)

            self.assertEqual(scanned, 3)
            self.assertEqual(
                findings,
                [
                    "README.md:1 states 'undecided'",
                    "agent.toml:1 states 'still open'",
                    "agent.toml:1 states 'resolve before release'",
                ],
            )


if __name__ == "__main__":
    unittest.main()
