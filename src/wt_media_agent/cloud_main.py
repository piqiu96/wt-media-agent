"""Cloud Agent entrypoint (`wt-media-cloud-agent`)."""

from __future__ import annotations

from wt_media_agent.bootstrap import cloud


def main() -> int:
    return cloud.run()


if __name__ == "__main__":
    raise SystemExit(main())
