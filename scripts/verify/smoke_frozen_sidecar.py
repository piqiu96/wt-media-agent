#!/usr/bin/env python3
"""Start a frozen Local Agent and prove its authenticated local API answers."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def free_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def probe(binary: Path, timeout: float) -> None:
    if not binary.is_file():
        raise FileNotFoundError(binary)
    port = free_loopback_port()
    token = secrets.token_urlsafe(24)
    with tempfile.TemporaryDirectory(prefix="wt-media-sidecar-smoke-") as data_dir:
        env = os.environ.copy()
        env.update({
            "WT_MEDIA_LOCAL_API_HOST": "127.0.0.1",
            "WT_MEDIA_LOCAL_API_PORT": str(port),
            "WT_MEDIA_AGENT_RUNTIME_TOKEN": token,
            "WT_MEDIA_AGENT_DATA_DIR": data_dir,
            "WT_MEDIA_AGENT_RUN_RUNNER": "false",
        })
        process = subprocess.Popen([str(binary)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"sidecar exited before answering healthz: {process.returncode}")
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/healthz",
                    headers={"Authorization": f"Bearer {token}"},
                )
                try:
                    with urllib.request.urlopen(request, timeout=1) as response:
                        body = json.load(response)
                    if body != {"status": "ok", "service": "wt-media-agent", "mode": "m1"}:
                        raise RuntimeError(f"unexpected healthz body: {body}")
                    print("healthz ok")
                    return
                except (urllib.error.URLError, TimeoutError, ConnectionError):
                    time.sleep(0.2)
            raise TimeoutError(f"sidecar did not answer authenticated healthz within {timeout}s")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    try:
        probe(args.binary, args.timeout)
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        parser.exit(1, f"frozen sidecar smoke failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
