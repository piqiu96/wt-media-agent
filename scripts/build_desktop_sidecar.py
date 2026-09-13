#!/usr/bin/env python3
"""Build a native frozen Local Agent binary for Tauri externalBin packaging."""

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
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    output = build(args.target, args.output_dir, args.manifest)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
