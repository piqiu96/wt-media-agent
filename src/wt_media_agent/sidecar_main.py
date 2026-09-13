"""Frozen Local Agent entrypoint used only by the Desktop sidecar."""

from __future__ import annotations

from wt_media_agent.local_api import server as local_api_server


def main() -> int:
    """Start a fixed loopback-only Local Agent API for Tauri.

    The Desktop Rust bridge owns the client connection.  A packaged sidecar must
    never inherit a host binding from an arbitrary customer environment.
    """

    return local_api_server.main(["--host", "127.0.0.1", "--port", "8765"])


if __name__ == "__main__":
    raise SystemExit(main())
