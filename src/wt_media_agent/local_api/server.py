"""Local Agent control API scaffold."""

from __future__ import annotations


class LocalApiServer:
    """Exposes health, environment, status, cancel, pull, and shutdown controls."""

    def health(self) -> dict[str, str]:
        return {"status": "ok"}
