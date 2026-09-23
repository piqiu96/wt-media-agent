"""Local Agent entrypoint (`wt-media-local-agent`)."""

from __future__ import annotations

from wt_media_agent.bootstrap import local


def main() -> int:
    return local.run()


if __name__ == "__main__":
    raise SystemExit(main())
