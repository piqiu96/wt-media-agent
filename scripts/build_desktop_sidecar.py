#!/usr/bin/env python3
"""Build a native frozen Local Agent binary for Tauri externalBin packaging.

Also stages the release configuration: `--config-dir` replaces the given
directory with a wholesale copy of `config_online/` (ADR-0016 §6). Desktop runs
that step on its own, after `cargo tauri build`, pointing at the app's
`Contents/Resources/config` (`scripts/stage-release-config.sh`) -- the order
matters because that directory is sealed by the app's signature. The frozen Agent
reads the configuration from there; see `runtime/config.py::default_config_dir`
for the other half of the contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def native_target(system: str | None = None, machine: str | None = None) -> str:
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    targets = {
        ("darwin", "arm64"): "aarch64-apple-darwin",
        ("darwin", "aarch64"): "aarch64-apple-darwin",
        ("darwin", "x86_64"): "x86_64-apple-darwin",
        ("windows", "amd64"): "x86_64-pc-windows-msvc",
        ("windows", "x86_64"): "x86_64-pc-windows-msvc",
    }
    try:
        return targets[(system, machine)]
    except KeyError as exc:
        raise ValueError(f"unsupported native sidecar platform: {system}/{machine}") from exc


def binary_name(target: str) -> str:
    suffix = ".exe" if target.endswith("windows-msvc") else ""
    return f"wt-media-agent-{target}{suffix}"


def pyinstaller_signing_args(system: str | None = None) -> list[str]:
    """Ad-hoc sign embedded Mach-O libraries for macOS runtime loading."""
    return ["--codesign-identity", "-"] if (system or platform.system()).lower() == "darwin" else []


def python_library_resign_args(system: str | None = None) -> list[str]:
    return ["codesign", "--force", "--sign", "-"] if (system or platform.system()).lower() == "darwin" else []


def python_shared_library() -> Path | None:
    library_dir = sysconfig.get_config_var("LIBDIR")
    library_name = sysconfig.get_config_var("LDLIBRARY")
    if not library_dir or not library_name:
        return None
    path = Path(library_dir) / library_name
    return path if path.is_file() else None


@contextmanager
def unsigned_python_shared_library():
    """Temporarily replace the interpreter's linker signature during freezing.

    PyInstaller embeds libpython but does not replace its linker signature. On
    recent macOS versions that signature can conflict with the ad-hoc-signed
    one-file process. The original file is restored before returning.
    """
    resign_args = python_library_resign_args()
    if not resign_args:
        yield
        return
    library = python_shared_library()
    if library is None:
        yield
        return
    with tempfile.TemporaryDirectory(prefix="wt-media-agent-python-lib-") as temporary:
        backup = Path(temporary) / library.name
        shutil.copy2(library, backup)
        has_signature = subprocess.run(
            ["codesign", "-d", str(library)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).returncode == 0
        try:
            if has_signature:
                subprocess.run([*resign_args, str(library)], check=True)
            yield
        finally:
            shutil.copy2(backup, library)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

CONFIG_SOURCE_DIR_NAME = "config_online"


def _files_under(root: Path) -> dict[str, bytes]:
    """Every file below `root`, keyed by relative path."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def mirror_differences(source: Path, destination: Path) -> list[str]:
    """Every way `destination` fails to be a byte-for-byte mirror of `source`.

    Empty means the two directories are interchangeable, which is the property
    the release is judged by (`diff -r config_online/ <product>/config` reports
    nothing). Named per difference rather than as one boolean, so a failure says
    which file went missing, arrived twice, or changed.
    """
    expected = _files_under(source)
    actual = _files_under(destination)
    differences = [f"missing {name}" for name in sorted(expected.keys() - actual.keys())]
    differences += [f"unexpected {name}" for name in sorted(actual.keys() - expected.keys())]
    differences += [
        f"changed {name}" for name in sorted(expected.keys() & actual.keys()) if expected[name] != actual[name]
    ]
    return differences


def sync_config(agent_root: Path, destination: Path) -> list[str]:
    """Replace `destination` with a wholesale copy of `agent_root/config_online`.

    Wholesale, not a merge: `config_online/` is the release replacement *source*
    (ADR-0016 §6), so a file deleted from it has to disappear from the product
    too. A merge would leave that file behind forever, and the mirror would only
    ever have been true on the day it was first copied.

    The copy is then read back and compared -- same relative names, same bytes --
    because "the product's `config/` can be diffed against `config_online/` and
    come out empty" is a property of the release, not of the copy call. A copy
    that cannot be read back is a release that must not be built.

    Returns the relative paths copied, so the caller can say what shipped.
    """
    source = agent_root / CONFIG_SOURCE_DIR_NAME
    if not source.is_dir():
        raise ValueError(f"no release config source at {source}")
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    differences = mirror_differences(source, destination)
    if differences:
        raise RuntimeError(
            f"{destination} is not a mirror of {source}: " + "; ".join(differences)
        )
    return sorted(_files_under(source))


def build(target: str, output_dir: Path, manifest: Path) -> Path:
    if target != native_target():
        raise ValueError(
            f"cross-platform sidecar build is not supported; requested {target}, native is {native_target()}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    name = binary_name(target)
    with tempfile.TemporaryDirectory(prefix="wt-media-agent-sidecar-") as temporary:
        workdir = Path(temporary)
        command = [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
            "--name", name, "--paths", str(ROOT / "src"),
            "--distpath", str(workdir / "dist"), "--workpath", str(workdir / "work"),
            "--specpath", str(workdir / "spec"), str(ROOT / "src" / "wt_media_agent" / "sidecar_main.py"),
        ]
        command[6:6] = pyinstaller_signing_args()
        with unsigned_python_shared_library():
            subprocess.run(command, check=True)
        built = workdir / "dist" / name
        if not built.is_file():
            raise RuntimeError(f"PyInstaller did not produce {built}")
        destination = output_dir / name
        shutil.copy2(built, destination)
    manifest.write_text(json.dumps({
        "component": "wt-media-agent",
        "version": PACKAGE_VERSION,
        "target": target,
        "filename": name,
        "sha256": sha256(destination),
    }, indent=2) + "\n", encoding="utf-8")
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default=native_target())
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help=(
            "directory to replace with a copy of config_online/. Desktop passes "
            "`<app>.app/Contents/Resources/config`, which exists only after Tauri has "
            "bundled the app, so this is also usable on its own."
        ),
    )
    args = parser.parse_args(argv)
    if (args.output_dir is None) != (args.manifest is None):
        parser.error("--output-dir and --manifest go together")
    if args.config_dir is None and args.output_dir is None:
        parser.error("nothing to do: pass --config-dir, or --output-dir and --manifest")

    # Before the freeze, not after: a release whose configuration cannot be
    # staged should fail in a second, not after a multi-minute PyInstaller run.
    if args.config_dir is not None:
        staged = sync_config(ROOT, args.config_dir)
        print(f"config-dir={args.config_dir} files={len(staged)}")
        for name in staged:
            print(f"config-file={name}")
    if args.output_dir is None:
        return 0
    output = build(args.target, args.output_dir, args.manifest)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
