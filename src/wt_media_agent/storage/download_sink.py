"""Where a download's bytes land on this machine, and how they become a file.

This holds no protocol and talks to nobody: it writes bytes to a path, and its
whole job is that a half-written download never looks like a finished one. The
bytes are streamed into `.wt-media-part/<task_id>.part` and reach their final
name only through `commit`, which fsyncs and then `os.replace`s. A `.part` that
never commits is left exactly as it was -- it is never renamed, and nothing
downstream can mistake it for the file.

Two placements are deliberate:

- **The part directory is inside the save directory**, not in the system temp
  directory. `os.replace` is only atomic within one filesystem, and a temp
  directory is routinely a different one (`/tmp` is often a separate mount, and
  on Windows it is usually another drive). A cross-device rename is not a rename
  at all: it is a copy, and a copy can be interrupted half-way, which is the one
  outcome this module exists to prevent.
- **The sink is bound to a directory, not to a task.** Every method takes the
  task id, because the caller is a download that knows its task and not its
  directory, and the directory is the thing an operator can change under it.

`FREE_SPACE_MARGIN_BYTES` exists because a check of "is there room for exactly
`total_bytes`" is wrong in the direction that hurts: the filesystem needs room
for its own bookkeeping, and the download itself is growing the part while the
check ages.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterator

#: Windows refuses these as file names, with or without an extension. The Agent
#: runs on macOS and Windows from one codebase, so the name is sanitized for the
#: stricter of the two: a title that is legal here and illegal there would fail
#: on the operator's machine rather than on ours.
RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{n}" for n in range(1, 10)]
    + [f"LPT{n}" for n in range(1, 10)]
)

#: Characters no mainstream filesystem accepts, plus the separator.
ILLEGAL_NAME_CHARACTERS = '<>:"/\\|?*'

#: Most filesystems cap one name at 255 *bytes* (not characters) of UTF-8.
MAX_NAME_BYTES = 255

#: Room kept for a " (99)" collision suffix when the base name is trimmed.
COLLISION_SUFFIX_BYTES = 6

#: The room `free_bytes()` insists on beyond the file itself.
FREE_SPACE_MARGIN_BYTES = 16 * 1024 * 1024

#: Bytes read at a time when a part file is read back for hashing. It is an I/O
#: buffer size and carries no meaning: nothing in this module decides anything
#: from how much was read.
PART_READ_CHUNK_BYTES = 1024 * 1024


class NameUnusableError(RuntimeError):
    """The title leaves no file name this machine will accept."""


class InsufficientSpaceError(RuntimeError):
    """The volume has no room for the file."""


class DownloadSink:
    """A save directory a download may write into."""

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)

    @property
    def directory(self) -> Path:
        return self._directory

    # ---- facts about the directory ----

    def is_writable(self) -> bool:
        return os.path.isdir(self._directory) and os.access(self._directory, os.W_OK)

    def free_bytes(self) -> int:
        """Free bytes on the directory's volume, or -1 when it cannot be read."""
        try:
            return shutil.disk_usage(self._directory).free
        except OSError:
            return -1

    def require_room(self, total_bytes: int) -> None:
        """Raise unless the volume has room for `total_bytes` and the margin.

        A `total_bytes` of `-1` means the source never declared a size. The check
        is skipped rather than failed: refusing a download because the server did
        not send a `Content-Range` would turn an inconvenience into a fault.
        """
        if total_bytes < 0:
            return
        free = self.free_bytes()
        if free < 0:
            return
        if free < total_bytes + FREE_SPACE_MARGIN_BYTES:
            raise InsufficientSpaceError(
                "the volume has no room for this file"
            )

    # ---- naming ----

    @staticmethod
    def file_name(title: str, material_id: int, extension: str) -> str:
        """A name for the file `title` describes, or raise `NameUnusableError`.

        The name is `<sanitized title>-<material id>.<extension>`. The id is in
        it because two materials may share a title and the operator needs the
        file to say which one it is; the title comes first because that is what
        a person scans a directory for.

        This sanitizes for both platforms at once (see `RESERVED_NAMES`). It is
        the reason the lease carries a verbatim title and not a name: the
        reserved names and separators are the filesystem's rules, so only the
        side that has a filesystem can apply them.
        """
        suffix = f"-{material_id}.{_normalize_extension(extension)}"
        budget = MAX_NAME_BYTES - len(suffix.encode("utf-8")) - COLLISION_SUFFIX_BYTES
        # One check, after the trim, because the trim is what decides: an empty
        # title stays empty through `_truncate_bytes`, and a title too long to
        # leave room for the suffix and the marker trims to nothing. A second
        # check before the budget was written first and then removed -- it could
        # not fail without the later one failing too, so nothing held it.
        stem = _truncate_bytes(_sanitize(title), budget)
        if not stem:
            raise NameUnusableError("the title leaves no usable file name")
        return f"{stem}{suffix}"

    def exists(self, name: str) -> bool:
        return (self._directory / name).exists()

    def allocate(self, name: str) -> str:
        """`name`, or the first `"<stem> (2).<ext>"` that is not taken yet.

        A second download of the same material must not overwrite the first, and
        it also must not fail: the operator asked for it, so they get both files
        and the platform's own convention for the second one.
        """
        if not self.exists(name):
            return name
        stem, dot, extension = name.rpartition(".")
        if not dot:
            stem, extension = name, ""
        for counter in range(2, 1000):
            marker = f" ({counter})"
            candidate_stem = _truncate_bytes(
                stem,
                MAX_NAME_BYTES
                - len(marker.encode("utf-8"))
                - len((dot + extension).encode("utf-8")),
            )
            candidate = f"{candidate_stem}{marker}{dot}{extension}"
            if not self.exists(candidate):
                return candidate
        raise NameUnusableError("every collision suffix for this name is taken")

    # ---- bytes ----

    def part_directory(self) -> Path:
        return self._directory / ".wt-media-part"

    def part_path(self, task_id: str) -> Path:
        return self.part_directory() / f"{_safe_task_id(task_id)}.part"

    def resume_offset(self, task_id: str) -> int:
        """Bytes already on disk for this task, as the part file measures them.

        The file is the authority, not the checkpoint: a checkpoint can be a
        little behind (it is written on a throttle) and a resumed download that
        trusted it would re-fetch bytes it already has, or worse, skip some.
        """
        try:
            return self.part_path(task_id).stat().st_size
        except OSError:
            return 0

    def part_chunks(
        self, task_id: str, chunk_bytes: int = PART_READ_CHUNK_BYTES
    ) -> Iterator[bytes]:
        """Iterate the bytes already in this task's part file, if there is one.

        A resumed attempt has to hash the bytes an earlier attempt wrote before it
        can hash any new ones -- a digest over the tail alone is a digest of
        nothing, and the check it feeds would pass on a file whose first half came
        from somewhere else. Nothing else reads the part back, which is why this
        is the one method here that does.

        A missing part iterates nothing rather than raising: the caller has just
        asked `resume_offset` and been told zero, and a download whose part was
        removed between the two calls has no bytes to continue from, which is the
        same thing.
        """
        try:
            handle = open(self.part_path(task_id), "rb")
        except OSError:
            return
        with handle:
            while True:
                chunk = handle.read(chunk_bytes)
                if not chunk:
                    return
                yield chunk

    def append(self, task_id: str, chunk: bytes) -> int:
        """Append `chunk` to the part file, creating it and its directory."""
        path = self.part_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab") as handle:
            return handle.write(chunk)

    def discard(self, task_id: str) -> None:
        """Throw the part away -- after a server ignored `Range`, say.

        A discard that raced with nothing is not an error: the caller discards
        because it must not resume, and a part that is already gone is the state
        it wanted.
        """
        path = self.part_path(task_id)
        try:
            path.unlink()
        except OSError:
            pass
        try:
            path.parent.rmdir()
        except OSError:
            pass

    def commit(self, task_id: str, name: str) -> Path:
        """Make the part file the real file, atomically, or leave it alone.

        The caller has already checked size and digest; this is only the rename.
        The part is fsynced first and the directory second, so a crash between
        the two leaves a file that is either absent or complete -- never
        present-and-empty, which is what a rename without the first fsync can
        produce.
        """
        part = self.part_path(task_id)
        final = self._directory / name
        handle = os.open(part, os.O_RDONLY)
        try:
            os.fsync(handle)
        finally:
            os.close(handle)
        os.replace(part, final)
        directory = os.open(self._directory, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        try:
            part.parent.rmdir()
        except OSError:
            pass
        return final


def _sanitize(title: str) -> str:
    """A title reduced to characters this filesystem will hold."""
    cleaned = []
    for character in title:
        # The control check comes first because `str.isspace()` is true for
        # several control characters (`\x1f` and `\x1c` among them), so testing
        # whitespace first would turn a control character into a space -- a
        # character the title never contained, invented by the sanitizer.
        if character in ILLEGAL_NAME_CHARACTERS or ord(character) < 0x20 or ord(character) == 0x7F:
            cleaned.append("_")
        elif character.isspace():
            cleaned.append(" ")
        else:
            cleaned.append(character)
    stem = "".join(cleaned).strip()
    # Windows silently strips trailing dots and spaces, so "a." and "a" name one
    # file; refusing both rather than letting the two spellings collide.
    stem = stem.rstrip(". ")
    # Nor may a name be a bare "." or "..", which name directories.
    if stem in (".", ".."):
        return ""
    if stem.upper() in RESERVED_NAMES:
        stem = f"_{stem}"
    return stem


def _normalize_extension(extension: str) -> str:
    """`".MP4"`, `"mp4"` and `""` -> `"mp4"`, `"mp4"`, `"bin"`."""
    cleaned = extension.strip().lstrip(".").lower()
    if not cleaned:
        return "bin"
    return "".join(
        character for character in cleaned if character.isalnum()
    ) or "bin"


def _truncate_bytes(text: str, budget: int) -> str:
    """`text` cut to at most `budget` UTF-8 bytes, on a character boundary."""
    if budget <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text
    # Cutting the bytes can leave a partial character, which `errors="ignore"`
    # drops -- so the result is always valid UTF-8 and never longer than asked.
    return encoded[:budget].decode("utf-8", errors="ignore").rstrip(". ")


def _safe_task_id(task_id: str) -> str:
    """A task id fit for a file name inside the part directory."""
    cleaned = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in task_id
    )
    return cleaned or "unknown"
