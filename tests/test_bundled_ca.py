"""The release CA is pinned at build time and selected before frozen HTTPS calls."""

from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from wt_media_agent.runtime import config

ROOT = Path(__file__).resolve().parents[1]


def build_script():
    spec = importlib.util.spec_from_file_location(
        "build_desktop_sidecar", ROOT / "scripts" / "build_desktop_sidecar.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BundledCABuildTests(unittest.TestCase):
    def test_standalone_macos_sidecar_avoids_hardened_runtime_team_mismatch(self) -> None:
        script = build_script()
        self.assertEqual(
            script.standalone_signing_args("darwin"),
            ["codesign", "--force", "--sign", "-", "--options=0"],
        )
        self.assertEqual(script.standalone_signing_args("windows"), [])

    def test_build_verifies_and_embeds_the_pinned_ca_and_notice(self) -> None:
        args = build_script().bundled_ca_args(ROOT)
        self.assertEqual(args.count("--add-data"), 3)
        self.assertIn(
            str(ROOT / "resources" / "ca-bundle.pem") + os.pathsep + "certs", args
        )
        self.assertIn(
            str(ROOT / "resources" / "ca-bundle.LICENSE") + os.pathsep + "certs", args
        )

    def test_build_rejects_a_changed_ca_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            resources = Path(tmp) / "resources"
            shutil.copytree(ROOT / "resources", resources)
            (resources / "ca-bundle.pem").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                build_script().bundled_ca_args(Path(tmp))

    def test_build_rejects_a_missing_license_notice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            resources = Path(tmp) / "resources"
            shutil.copytree(ROOT / "resources", resources)
            (resources / "ca-bundle.LICENSE").unlink()
            with self.assertRaisesRegex(ValueError, "license"):
                build_script().bundled_ca_args(Path(tmp))


class FrozenCATests(unittest.TestCase):
    def test_frozen_startup_selects_the_embedded_ca(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            certs = Path(tmp) / "certs"
            certs.mkdir()
            shutil.copy2(ROOT / "resources" / "ca-bundle.pem", certs / "ca-bundle.pem")
            environment: dict[str, str] = {}
            selected = config.configure_frozen_ca_bundle(
                frozen=True, bundle_root=Path(tmp), environment=environment
            )
            self.assertEqual(selected, certs / "ca-bundle.pem")
            self.assertEqual(environment["SSL_CERT_FILE"], str(selected))

    def test_explicit_ca_override_is_kept(self) -> None:
        selected = ROOT / "resources" / "ca-bundle.pem"
        environment = {"SSL_CERT_FILE": str(selected)}
        actual = config.configure_frozen_ca_bundle(
            frozen=True, bundle_root=Path("/missing-bundle"), environment=environment
        )
        self.assertEqual(actual, selected)
        self.assertEqual(environment["SSL_CERT_FILE"], str(selected))

    def test_missing_embedded_ca_fails_frozen_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "CA bundle"):
                config.configure_frozen_ca_bundle(
                    frozen=True, bundle_root=Path(tmp), environment={}
                )

    def test_source_run_keeps_existing_system_trust(self) -> None:
        environment: dict[str, str] = {}
        self.assertIsNone(config.configure_frozen_ca_bundle(
            frozen=False, bundle_root=Path("/missing-bundle"), environment=environment
        ))
        self.assertNotIn("SSL_CERT_FILE", environment)


if __name__ == "__main__":
    unittest.main()
