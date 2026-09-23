"""Local SQLite and file index storage.

The package surface re-exports the checkpoint store's public types so callers
do not have to reach into `storage.checkpoint_store`. `storage.migration` is
deliberately not re-exported: it is a frozen CLI entrypoint whose module path
`wt_media_agent.storage.migration` is invoked by `scripts/migrate-storage.sh`.
"""

from __future__ import annotations

from wt_media_agent.storage.checkpoint_store import (
    CheckpointStore,
    OfflineResult,
    TaskCheckpoint,
)

__all__ = ["CheckpointStore", "OfflineResult", "TaskCheckpoint"]
