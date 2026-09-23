"""Agent process assembly and the mode surfaces built on it.

Import a surface, not this package: `bootstrap.local`, `bootstrap.cloud`,
`bootstrap.sidecar`. `bootstrap.app.build_components` is the shared assembly
every one of them starts from (ADR-0016 §2).
"""

from __future__ import annotations

from wt_media_agent.bootstrap.app import Components, build_components, database_path

__all__ = ["Components", "build_components", "database_path"]
