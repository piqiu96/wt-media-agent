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
import tempfile
import tomllib
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
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
            "--name", name, "--paths", str(ROOT / "src"),
            "--distpath", str(workdir / "dist"), "--workpath", str(workdir / "work"),
            "--specpath", str(workdir / "spec"), str(ROOT / "src" / "wt_media_agent" / "sidecar_main.py"),
        ], check=True)
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
