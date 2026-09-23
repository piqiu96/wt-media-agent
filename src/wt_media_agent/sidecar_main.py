"""Frozen Local Agent entrypoint used only by the Desktop sidecar.

`scripts/build_desktop_sidecar.py` passes this file to PyInstaller as the entry
script, so the path and the module name are frozen. Desktop passes the loopback
host, the port, the runtime token and the data directory as environment
variables; `runtime/config.py` reads them. No parameter reaches this process
through a command line, so the token is not visible in `ps`.

The `local_api_server` attribute is kept because it is the name the entry test
pins; the call now goes through `bootstrap.sidecar`, which is the assembly entry
ADR-0016 §2 designates.
"""

from __future__ import annotations

from wt_media_agent.bootstrap import sidecar
from wt_media_agent.local_api import server as local_api_server

__all__ = ["local_api_server", "main"]


def main() -> int:
    return sidecar.run()


if __name__ == "__main__":
    raise SystemExit(main())
