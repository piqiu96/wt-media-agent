"""The one directory this machine downloads into, and where the choice lives.

The choice is *stored* rather than handed to the download as an argument,
because the Agent is a sidecar the Desktop starts and stops. The operator picks
a directory once; the Desktop pushes it on every start; the Agent keeps it. An
Agent that held it only in memory would lose the choice on the first restart and
-- worse -- would report the loss as "nothing chosen", which is not a state the
operator ever reached.

Only the key and the two operations live here. What makes a directory *usable*
belongs to `DownloadSink`, because that is the side that has a filesystem, and
because the answer moves: a directory can be unmounted, replaced by a file or
filled up long after it was chosen. So `writable` and `free_bytes` are read when
they are asked for and are never stored.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from wt_media_agent.storage.sqlite import connect
from wt_media_agent.utils.time import utc_now_iso

#: The `agent_metadata` key the operator's choice is stored under. Named rather
#: than written out at both ends: the local API writes it and the download
#: executor reads it, in two different processes, so a typo in one of them would
#: be a download that never finds its directory and no error anywhere.
SAVE_DIRECTORY_KEY = "download.save_directory"


class SaveDirectoryStore:
    """Reads and writes the stored save-directory choice.

    The value is a path on this machine and is never sent anywhere: the local API
    answers the Desktop with it because the Desktop put it there, and the Cloud
    never hears it (CHG-061 §4).
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        return connect(self._db_path)

    def get(self) -> Optional[str]:
        """The chosen directory, or `None` when none has been stored.

        A row holding an empty string reads as `None` too: `agent_metadata.value`
        is `NOT NULL`, and a caller that wrote "" meant no choice, not a
        directory named nothing.
        """
        with self._connect() as db:
            row = db.execute(
                "SELECT value FROM agent_metadata WHERE key = ?", (SAVE_DIRECTORY_KEY,)
            ).fetchone()
        if row is None:
            return None
        return str(row["value"]) or None

    def set(self, directory: str) -> None:
        """Store `directory` as this machine's choice, replacing any previous one.

        Upsert rather than insert: the Desktop pushes the same choice on every
        start, so a second push of an unchanged directory must be an ordinary
        success and not a uniqueness failure the operator would see as "could not
        save your folder".
        """
        with self._connect() as db:
            db.execute(
                "INSERT INTO agent_metadata (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (SAVE_DIRECTORY_KEY, directory, utc_now_iso()),
            )
