"""What a downloading attempt leaves behind so the next one can continue it.

A material download is long, the Agent is restarted at will, and the task it is
working on lives in Cloud -- so an attempt that dies half-way has to come back
to the same file, not to a new one. Two facts are needed for that and no more:

- **`file_name`** -- the final name the attempt committed to. Re-deriving it is
  not the same answer: `DownloadSink.allocate` picks the first name that is free
  *now*, so a second attempt running after some other download took the name
  would write a second file for the same task. The operator watching the folder
  would see one download become two.
- **`bytes_done`** -- what the attempt has already told Cloud. A resumed attempt
  whose first progress report went backwards would show the operator a rewind
  that never happened, and would read as a stall to anything comparing two
  consecutive reports.

**What is deliberately absent: the byte offset the resumed attempt asks for.**
That comes from the part file's own size (`DownloadSink.resume_offset`), which
cannot claim more than the bytes actually on disk. A stored count can: a
checkpoint written and a tail of the part file lost (an unflushed buffer, a
killed process) would make the next attempt request `bytes=<stored>-` and skip
exactly the missing bytes -- a file of the right length and the wrong content,
which nothing downstream notices until the digest. Reading the size back costs
one `stat`.

**Also deliberately absent: any path.** The part file's location is derived when
the resume happens, from the save directory in force *then*, so an operator who
changes the directory mid-download loses nothing in progress. A stored path
would pin the download to a directory that may no longer exist, and would be the
one place a local absolute path reached a database -- CHG-061 §8 keeps paths out
of what this Agent records. `decode` therefore refuses a name that is a path.

The record travels as a JSON object in `task_checkpoints.transfer_state_json`
(migration `0003_transfer_resume`), which is a plain nullable `TEXT`: this module
owns the content, the checkpoint store owns the storage, and neither has to know
the other's rules.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

#: A resume record carries exactly these, and a build that reads one it does not
#: understand reads it as "no resume" rather than guessing at the rest.
RESUME_FIELDS = ("file_name", "bytes_done")

#: Names that are not names. `/` and `\` are separators on the two platforms the
#: Agent ships on, and `.`/`..` are directories.
_NOT_A_NAME = (".", "..")


@dataclass(frozen=True)
class TransferResume:
    """The durable half of a download: where it was going, and how far it got."""

    file_name: str
    bytes_done: int

    def encode(self) -> str:
        """The stored form. Key order is fixed so two equal records are equal text.

        `ensure_ascii=False`: a material title is usually CJK, and a row that
        reads `\\u6625\\u65e5-42.mp4` is no use to the person looking at the
        database to find out which download stalled. SQLite holds UTF-8 as it
        is, and both ends of this format are `json`, so nothing is lost.
        """
        return json.dumps(
            {"file_name": self.file_name, "bytes_done": self.bytes_done},
            ensure_ascii=False,
        )

    @classmethod
    def decode(cls, raw: Optional[str]) -> Optional["TransferResume"]:
        """The record in `raw`, or `None` when there is nothing usable in it.

        Unusable covers both "absent" (a first attempt) and "unreadable" (a row
        an older build or a hand edit left behind). Both answer the same way on
        purpose: a download that cannot read its resume state re-fetches from
        zero, which is always safe because size and digest are checked at the
        end, whereas refusing would be a download that never starts.
        """
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        name = payload.get("file_name")
        done = payload.get("bytes_done")
        if not isinstance(name, str) or not cls._is_a_name(name):
            return None
        # `isinstance(True, int)` is true, and `{"bytes_done": true}` has never
        # meant one byte.
        if not isinstance(done, int) or isinstance(done, bool) or done < 0:
            return None
        return cls(name, done)

    @staticmethod
    def _is_a_name(name: str) -> bool:
        """Is `name` one path component rather than a location?

        Checked here rather than left to the caller: this is the single point
        where a stored string becomes a name, and the only place a separator can
        be caught before something joins it to a directory.
        """
        if not name or name in _NOT_A_NAME:
            return False
        return "/" not in name and "\\" not in name
