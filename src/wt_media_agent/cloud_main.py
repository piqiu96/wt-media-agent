"""Cloud Agent entrypoint."""

from __future__ import annotations

from wt_media_agent.app import create_app


def main() -> int:
    return create_app("cloud").run()


if __name__ == "__main__":
    raise SystemExit(main())
