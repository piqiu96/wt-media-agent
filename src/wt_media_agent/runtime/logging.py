"""Centralized logging configuration for the Agent.

Three files, written by one entry point (CHG-057 T-04; the user's ruling 三):

- `agent.log` -- everything, and the only file a traceback appears in.
- `task.log`  -- the task narrative: records from `wt_media_agent.runner.*`.
- `error.log` -- ERROR and above, as a record rather than a stack dump, carrying
  `error_code` / `task_id` / `context` beside the message (T-05).

They are plain text with one line per record; the ruling explicitly does not
want a JSON log system. `configure_from` is the single caller (bootstrap), and
the stderr handler is installed unconditionally, so a log directory that cannot
be written degrades to the terminal instead of to silence.

Bounded four ways (T-07; the ruling 六): a file rolls on a date change and when
it would pass 20 MB, a single oversized record is truncated and marked rather
than allowed to grow the file, and history is pruned by age (14 days) and by the
three files' shared budget (400 MB).

Moved here from `wt_media_agent/log_setup.py` (CHG-056 T-03): log setup is
runtime-layer work (ADR-0016 §4).
"""

from __future__ import annotations

import logging
import logging.config
import os
import re
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from wt_media_agent.runtime.config import SENSITIVE_KEY_NAMES, AgentConfig
from wt_media_agent.runtime.constants import (
    DEFAULT_LOG_MAX_BYTES,
    DEFAULT_LOG_RETENTION_DAYS,
    DEFAULT_LOG_TOTAL_BYTES,
)

#: Matches the package the modules actually log under (`getLogger(__name__)`
#: produces `wt_media_agent.<module>`). The pre-T-03 default was the hyphenated
#: `wt-media-agent`, which matches no logger in the codebase -- harmless only
#: because the function never ran.
DEFAULT_COMPONENT = "wt_media_agent"

AGENT_LOG_NAME = "agent.log"
TASK_LOG_NAME = "task.log"
ERROR_LOG_NAME = "error.log"

#: Where the task narrative comes from. Matched on a dotted boundary, so a
#: future `wt_media_agent.runner_pool` is a different component and stays out.
TASK_LOGGER_PREFIX = "wt_media_agent.runner"

#: Every record routed into the three files. `stderr` deliberately has none:
#: the terminal must show what the files show, filters and all.
FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATEFMT = "%Y-%m-%dT%H:%M:%S"

#: The ruling's fields for `error.log` (三). They are carried by `extra={...}`
#: on the logging call and defaulted by the formatter. `error_code` is required
#: there, so an event that has no code says so rather than dropping the field;
#: `task_id` and `context` are optional and are left out when unset.
ERROR_CODE_FIELD = "error_code"
TASK_ID_FIELD = "task_id"
CONTEXT_FIELD = "context"

#: What a required field reads as when the record has no value for it. Not the
#: empty string: a reader must be able to tell "this event has no code" from
#: "the field never made it into the line".
NO_VALUE = "none"

# The codes that exist today, and there is no registry behind them (CHG-057
# measured that). This Task builds the channel, not the vocabulary, so nothing
# validates a code and a new one needs no change here. Current sources:
# `runner/runner.py` -- `no_executor`, `session_invalidated_result_uncertain`,
# `executor_error` (the code its checkpoint records for any executor failure);
# `contracts/local-error-codes/v1/bitbrowser.yaml` -- `not_found`,
# `bitbrowser_identity_unverifiable`, `bitbrowser_response_error`.

#: The one place the fields are rendered. `agent.log` and `task.log` keep `FMT`:
#: `agent.log` is the full-fidelity file, and the message already names the task.
ERROR_FMT = (
    f"%(asctime)s [%(levelname)s] %(name)s: {ERROR_CODE_FIELD}=%(error_code)s"
    f"%(extra_fields)s %(message)s"
)


REDACTED = "***"

#: A shorter value is not used as a literal needle: redacting a 3-character
#: "secret" would blank out ordinary prose wherever those letters appear, which
#: costs the log its purpose without protecting anything worth protecting.
MIN_SECRET_LENGTH = 8

#: The credential vocabulary is the config loader's, not a second copy of it:
#: a name that refuses a value in a shipped TOML file must also mask that value
#: in a log line. The key has to *be* one of those names -- not merely look like
#: `something: something` -- or a scheme in a URL (`https:`) matches first and
#: swallows the credential that follows it. A prefix is allowed, in either
#: spelling, so `proxy_password`, `set-cookie` and `X-Api-Key` are keys too.
_LEAF = "|".join(
    re.escape(variant)
    for name in sorted(SENSITIVE_KEY_NAMES, key=len, reverse=True)
    for variant in (name, name.replace("_", "-"))
)
_KEY = rf"[A-Za-z0-9_.\-]*?(?:{_LEAF})"

_BEARER_SCHEMES = "Bearer|Basic|Token|Digest"
_TOKEN = r"[A-Za-z0-9\-._~+/=]{8,}"
_QUOTED_OR_TOKEN = r"\"[^\"]*\"|'[^']*'|[^\s,;\"'&}\]]+"

_LEADING_VALUE = re.compile(rf"(?:(?:{_BEARER_SCHEMES})\s+)?(?:{_QUOTED_OR_TOKEN})")

#: Two patterns, split by how far each key family's value reaches -- and one
#: callback each, so the two passes are independent of their order.
#:
#: A cookie header's tail and an `Authorization` value both run to the end of the
#: line: they carry several credentials of their own (`a=1; session=xyz`, and a
#: scheme word in front of a blob), so their pattern takes the whole tail.
_TAIL_KEYED = re.compile(rf"(?i)\b({_KEY})[\"']?(\s*[:=]\s*)([^\n]*)")
#: Every other family takes exactly one leading token, and the match *ends* with
#: it. That bound is the whole point: `re.sub` resumes scanning after the match,
#: so a value that reached to the end of the line would swallow whatever follows
#: it and stop the scan -- `token=aaa password=bbb` masked the first credential,
#: swallowed the second, and left it in the log.
_VALUE_KEYED = re.compile(rf"(?i)\b({_KEY})[\"']?(\s*[:=]\s*)({_LEADING_VALUE.pattern})")
_BEARER = re.compile(rf"(?i)\b({_BEARER_SCHEMES})\s+({_TOKEN})")
#: A JWT is three base64url segments; `eyJ` is the encoding of `{"`, which is
#: what every JOSE header starts with. Narrow on purpose: a blanket "long random
#: string" rule would redact ids and hashes, so a bare 64-hex secret with no key
#: and no JWT shape is *not* caught here (recorded as a known limit).
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+")
#: `https://user:password@host` -- the password, not the user, is the credential.
_USERINFO = re.compile(r"://([^/\s:@]+):([^/\s@]+)@")


def _normalized(key: str) -> str:
    return key.strip().lower().replace("-", "_")


def _is_sensitive_key(key: str) -> bool:
    normalized = _normalized(key)
    if normalized in SENSITIVE_KEY_NAMES:
        return True
    return any(normalized.endswith("_" + name) for name in SENSITIVE_KEY_NAMES)


def _is_cookie_key(key: str) -> bool:
    # Both spellings, because the vocabulary has both: `cookie` and `cookies` are
    # each a name the config loader refuses a value for. Matching only the
    # singular sent the plural down the generic path, which stops at the first
    # `;` and left every later pair of the header in the log.
    normalized = _normalized(key)
    return normalized.endswith("cookie") or normalized.endswith("cookies")


def _is_opaque_key(key: str) -> bool:
    """Keys whose value is the credential and may contain separators of its own."""
    return _normalized(key).endswith("authorization")


def _quote_like(value: str) -> str:
    """Mask a value, keeping the quotes it arrived in (so JSON stays JSON)."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return f"{value[0]}{REDACTED}{value[-1]}"
    return REDACTED


def _mask_cookie_pairs(value: str) -> str:
    """Mask each `name=value`'s value, keeping the names and the separators.

    A cookie header holds several credentials at once (`a=1; session=xyz`), so
    masking only the first would leak the session. The names are kept: they are
    the part that tells a reader *which* cookie was in play.
    """
    masked: list[str] = []
    for part in value.split(";"):
        name, separator, _rest = part.partition("=")
        if separator:
            masked.append(f"{name}={REDACTED}")
        else:
            masked.append(REDACTED if part.strip() else part)
    return ";".join(masked)


def _mask_tail_keyed(match: re.Match[str]) -> str:
    """Mask a family whose value runs to the end of the line.

    Every other family is left to `_mask_value_keyed`; returning the match
    unchanged here is what keeps this pass from reaching past its own families.
    """
    key, separator, tail = match.groups()
    if _is_cookie_key(key):
        return f"{key}{separator}{_mask_cookie_pairs(tail)}"
    if _is_opaque_key(key):
        return f"{key}{separator}{REDACTED}"
    return match.group(0)


def _mask_value_keyed(match: re.Match[str]) -> str:
    """Mask one bounded value, echoing the key exactly as it arrived.

    The echo is the matched text up to the value, not `key + separator` rebuilt
    from the groups. The pattern has to step over the closing quote of a JSON key
    to reach the separator -- `"client_secret": ` -- so a rebuilt prefix drops
    that quote and writes `{"client_secret: "***"}`, a line that is no longer
    JSON and no longer says which field was masked.
    """
    key, _separator, value = match.groups()
    if _is_cookie_key(key) or _is_opaque_key(key) or not _is_sensitive_key(key):
        return match.group(0)
    prefix = match.group(0)[: match.start(3) - match.start()]
    return f"{prefix}{_quote_like(value)}"


def _mask_known(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if len(secret) >= MIN_SECRET_LENGTH:
            text = text.replace(secret, REDACTED)
    return text


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Mask credentials in one line of text, keeping everything around them.

    Table-driven over shapes rather than words: `Name: value`, `Name=value`,
    `"Name": "value"`, a query string, a bare bearer/JWT blob, URL userinfo, and
    the literal value of a secret this process holds (`secrets` -- the local
    runtime token is passed in by `configure_from`).

    Masking is idempotent, because a record is formatted once per handler.
    """
    text = _VALUE_KEYED.sub(_mask_value_keyed, text)
    text = _TAIL_KEYED.sub(_mask_tail_keyed, text)
    text = _USERINFO.sub(lambda m: f"://{m.group(1)}:{REDACTED}@", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _JWT.sub(REDACTED, text)
    return _mask_known(text, secrets)


class TaskLogFilter(logging.Filter):
    """Keep `task.log` to the runner's records."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name == TASK_LOGGER_PREFIX or record.name.startswith(
            TASK_LOGGER_PREFIX + "."
        )


class ErrorLogFilter(logging.Filter):
    """Keep `error.log` to ERROR and above."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.ERROR


class RedactingFormatter(logging.Formatter):
    """Mask credentials in the *rendered* line (the ruling 十, 十三·7).

    Rendering, not the record, is where the mask has to be applied. A
    `logging.Filter` that rewrote `record.msg` would leave the traceback
    untouched -- and a traceback is exactly where a credential turns up
    (`ValueError(f"rejected {token}")`, a URL with userinfo, a request dump).
    Masking the finished text covers message and traceback alike, in one place
    that every file handler and the terminal share.

    `secrets` are values this process holds, masked verbatim wherever they
    appear, so a credential that reaches a message with nothing naming it is
    still caught.
    """

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        style: str = "%",
        secrets: Iterable[str] = (),
    ) -> None:
        super().__init__(fmt, datefmt, style)
        self.secrets = tuple(s for s in secrets if len(s) >= MIN_SECRET_LENGTH)

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self.secrets)


class NoTracebackFormatter(RedactingFormatter):
    """Format a record without its exception text (ruling 三).

    `error.log` carries the failure as a record; the full traceback belongs to
    `agent.log`, whose formatter is the standard one.

    Formatting a *copy* is the point. A record is a single object handed to
    every handler, and `Formatter.format` caches the rendered traceback on
    `record.exc_text` -- so overriding only `formatException` would still let
    the traceback through here whenever a standard formatter had already
    rendered the same record (which is the order `configure_logging` installs).
    Clearing the fields on the record itself would be worse: it would strip the
    traceback from `agent.log` too.
    """

    def isolate(self, record: logging.LogRecord) -> logging.LogRecord:
        """A copy of the record with everything that renders a traceback cleared."""
        isolated = logging.makeLogRecord(record.__dict__)
        isolated.exc_info = None
        isolated.exc_text = None
        isolated.stack_info = None
        return isolated

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        """The record as this formatter wants to render it. Subclass hook."""
        return self.isolate(record)

    def format(self, record: logging.LogRecord) -> str:
        # The base *class* call for the rendering, so the record is isolated
        # once: `super().format` would be `RedactingFormatter.format`, which
        # renders first and masks after -- the same order, but it would isolate
        # again on the way.
        rendered = logging.Formatter.format(self, self.prepare(record))
        return redact(rendered, self.secrets)


def _field(record: logging.LogRecord, name: str) -> str:
    """One field's value as a single line of text, or "" if the record has none.

    Newlines are escaped, not stripped: `task_id` arrives from Cloud and is
    interpolated verbatim, so a value carrying a newline would otherwise forge a
    record in a file whose whole contract is one line per record. Values are
    expected to be tokens; T-06 owns redacting anything sensitive inside them.
    """
    value = getattr(record, name, "")
    if not value:
        return ""
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


class ErrorRecordFormatter(NoTracebackFormatter):
    """`error.log`'s line: the record's fields, then the message (CHG-057 三).

    The defaults live here rather than in `setLogRecordFactory`, because the two
    halves of the convention cannot coexist: a factory that pre-populated
    `error_code` on every record would make `Logger.makeRecord` reject
    `extra={"error_code": ...}` with a KeyError -- the very call this channel is
    built on. The tests assert that collision so the choice stays a decision.
    """

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        isolated = self.isolate(record)
        isolated.error_code = _field(record, ERROR_CODE_FIELD) or NO_VALUE
        isolated.extra_fields = "".join(
            f" {name}={_field(record, name)}"
            for name in (TASK_ID_FIELD, CONTEXT_FIELD)
            if _field(record, name)
        )
        return isolated


def _report(message: str) -> None:
    """Tell the operator something without going back through the logging system.

    This runs on the emit path -- a delete that fails leaves the log growing,
    which is the failure T-07 exists to prevent, so it cannot be swallowed
    quietly; but it also cannot be logged, because the handler being written is
    one of the handlers that record would go to. stderr is the only destination
    that is both visible and safe (`logging.Handler.handleError` makes the same
    choice, for the same reason).
    """
    if logging.raiseExceptions:
        sys.stderr.write(f"wt-media-agent logging: {message}\n")


def _utc_date(stamp: float) -> date:
    """The date a moment belongs to, in UTC.

    UTC rather than local time: the rolled name has to keep its meaning across a
    timezone change (a laptop that crosses a border does not get a second day),
    and the retention window is compared against the same clock the names use.
    """
    return datetime.fromtimestamp(stamp, tz=timezone.utc).date()


class LogBudget:
    """What the three files may occupy together, and how long they may stay.

    Shared by the three handlers on purpose: the total (400 MB as shipped) is the
    *Agent's* footprint, and one budget per file would silently multiply it by
    three. It also owns the clock, so a test that moves time moves the naming
    rule and the retention rule at once.

    It deletes rolled files only. The file a handler is currently writing is
    never a candidate (the ruling 六: 当前打开的文件永不被删) -- a rolled file is
    history, the live one is the only copy of what just happened.

    What the total bound is, exactly: pruning happens at a roll and at startup,
    so between two prunes the three open files may each still grow to their own
    cap. The on-disk total is therefore bounded by `total_bytes + 3 * max_bytes`
    (400 MB + 60 MB as shipped), not by `total_bytes` to the byte. The property
    the ruling asks for -- 总容量受限, a log that does not grow without bound --
    holds either way; what would not hold is a claim of exactness, and the
    measured overshoot is pinned by `TotalBudgetTest`.
    """

    def __init__(
        self,
        directory: Path,
        *,
        total_bytes: int = DEFAULT_LOG_TOTAL_BYTES,
        retention_days: int = DEFAULT_LOG_RETENTION_DAYS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.directory = Path(directory)
        self.total_bytes = total_bytes
        self.retention_days = retention_days
        self.clock = clock
        self._live: set[Path] = set()
        self._families: list[re.Pattern[str]] = []

    def today(self) -> date:
        return _utc_date(self.clock())

    def register(self, live: Path) -> None:
        """Declare one file that is currently open, and so never prunable.

        Idempotent, because `configure_logging` declares the three files before
        the first prune -- pruning has to know the family names, or it matches
        nothing and quietly deletes nothing (measured: with the shipped caps, a
        real run left 1999-dated files in place for its whole lifetime) -- and
        each handler declares its own again when it is constructed.
        """
        if live in self._live:
            return
        self._live.add(live)
        # `agent.log` -> `agent-YYYYMMDD-N.log`. The stem keeps the family, the
        # index separates two rolls on one day, and the suffix keeps the name
        # recognisable as a log to whoever is looking at the directory.
        self._families.append(
            re.compile(
                rf"^{re.escape(live.stem)}-(\d{{8}})-(\d+){re.escape(live.suffix)}$"
            )
        )

    def rolled(self) -> list[tuple[Path, date, int]]:
        """This budget's rolled files, oldest first."""
        found: list[tuple[Path, date, int]] = []
        for path in self.directory.iterdir():
            if path in self._live or not path.is_file():
                continue
            for pattern in self._families:
                match = pattern.match(path.name)
                if match:
                    found.append(
                        (
                            path,
                            datetime.strptime(match.group(1), "%Y%m%d").date(),
                            int(match.group(2)),
                        )
                    )
                    break
        # Index as the tie-break, so two rolls on one day retire in the order
        # they were written rather than in whatever order the directory lists.
        found.sort(key=lambda item: (item[1], item[2]))
        return found

    def prune(self) -> list[Path]:
        """Delete what is out of the window, then what is over the budget.

        Returns what it deleted, so the decision is visible to a caller (and to
        a test) instead of being inferred from the directory afterwards.
        """
        deleted: list[Path] = []
        cutoff = self.today() - timedelta(days=self.retention_days)
        for path, when, _index in self.rolled():
            # `<=`: a 14-day window is today plus the 13 days before it. A file
            # dated exactly `retention_days` ago is the 15th day and goes.
            if when <= cutoff and self._remove(path):
                deleted.append(path)

        remaining = self.rolled()
        total = sum(self._size(path) for path in self._live)
        total += sum(self._size(path) for path, _when, _i in remaining)
        while total > self.total_bytes and remaining:
            path, _when, _index = remaining.pop(0)
            size = self._size(path)
            if self._remove(path):
                deleted.append(path)
                total -= size
        return deleted

    def _remove(self, path: Path) -> bool:
        try:
            path.unlink()
        except OSError as exc:
            # Not fatal to the process, but not quiet either: if the deletion
            # never succeeds the log has no bound left, and a reader wondering
            # why the directory is full needs this line.
            _report(f"cannot delete {path} ({type(exc).__name__}: {exc})")
            return False
        return True

    @staticmethod
    def _size(path: Path) -> int:
        """A file's size, or 0 if it is already gone.

        The three handlers share this budget and run in different threads, so a
        file can be listed by one prune and renamed away by another handler's
        roll before it is sized. A missing file contributes nothing to the
        total, and letting the `FileNotFoundError` out would abort the prune
        *and* lose the record whose emit triggered it.
        """
        try:
            return path.stat().st_size
        except OSError:
            return 0


class BoundedFileHandler(logging.Handler):
    """One plain-text log file, bounded four ways (the ruling 六).

    Replaces `RotatingFileHandler`: the stdlib policy is `backupCount` files of
    `maxBytes` each, which expresses none of the four bounds -- no time window,
    no total budget, and an oversized single record is written whole, taking the
    file past its own cap.

    The four bounds here: the file rolls on a date change, rolls when the next
    record would take it past `max_bytes`, truncates a record that cannot fit,
    and defers history to the shared `LogBudget`.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = DEFAULT_LOG_MAX_BYTES,
        budget: LogBudget,
        encoding: str = "utf-8",
    ) -> None:
        super().__init__()
        self.base_path = Path(path)
        # At least one byte, so the arithmetic below cannot be handed a limit of
        # zero by a caller that skipped the configuration layer's validation.
        self.max_bytes = max(1, max_bytes)
        self.budget = budget
        self.encoding = encoding
        self._stream = None
        self._open_date = budget.today()
        budget.register(self.base_path)
        # Opened eagerly, like `FileHandler` without `delay`: the Agent must not
        # be able to start and look as though it has no log configured.
        self._open()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self._stream is None:
                self._open()
            line = self._bounded(self.format(record)) + "\n"
            # Bounded first, then asked where it goes: a record too large for an
            # empty file is truncated rather than rolled, which is what keeps an
            # oversized record from starting a file of its own (the ruling 六).
            self._roll_if_needed(len(line.encode(self.encoding)))
            self._stream.write(line)
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
        finally:
            super().close()

    def _bounded(self, text: str) -> str:
        """The record as it will be written: truncated to one file's worth.

        The cap for a single record *is* the file cap. A record larger than an
        empty file could never be written at all, and picking some smaller number
        would invent a bound the ruling does not have. The marker carries the
        size the record *had*, which is the only way a reader can tell a
        truncated line from a short one.
        """
        raw = text.encode(self.encoding)
        limit = self.max_bytes - 1  # `emit` appends the newline that ends the line
        if len(raw) <= limit:
            return text
        marker = f" truncate=true original_size={len(raw)}"
        room = max(limit - len(marker.encode(self.encoding)), 0)
        # `errors="ignore"`: the cut is on a byte boundary, so it may land inside
        # a character. Dropping that one character is the point -- emitting half
        # of it would put a replacement byte in a plain-text file.
        head = raw[:room].decode(self.encoding, errors="ignore")
        return head + marker

    def _roll_if_needed(self, size: int) -> None:
        """Roll if this record does not belong in the open file."""
        today = self.budget.today()
        current = self._size()
        if today == self._open_date and current + size <= self.max_bytes:
            return
        if current == 0:
            # Nothing to preserve: an empty file is not history, and rolling it
            # would manufacture empty files for a directory left over from
            # yesterday. Only the date moves.
            self._open_date = today
            return
        self._roll()
        self._open_date = today

    def _roll(self) -> None:
        self._stream.close()
        self._stream = None
        target = self._free_name(self._open_date)
        try:
            # Onto a name this handler just proved free: the index is chosen by
            # looking, and one handler at a time writes here.
            self.base_path.rename(target)
        except OSError as exc:
            # Reopen the live file before re-raising, so the handler is not left
            # with no stream at all; the record that triggered this is reported
            # through `handleError` by `emit`.
            self._open()
            raise OSError(f"cannot roll {self.base_path} to {target}: {exc}") from exc
        self._open()
        # After the roll, not before: the new live file has to exist when the
        # budget counts what is on disk.
        self.budget.prune()

    def _free_name(self, when: date) -> Path:
        stamp = when.strftime("%Y%m%d")
        index = 1
        while True:
            candidate = self.base_path.with_name(
                f"{self.base_path.stem}-{stamp}-{index}{self.base_path.suffix}"
            )
            if not candidate.exists():
                return candidate
            index += 1

    def _size(self) -> int:
        if self._stream is None:
            return 0
        # `fstat`, not `tell()`: on a text stream `tell` is documented as an
        # opaque cookie, and this number is compared against a byte cap.
        return os.fstat(self._stream.fileno()).st_size

    def _open(self) -> None:
        self.base_path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.base_path.open("a", encoding=self.encoding)


def configure_logging(
    level: str = "INFO",
    log_file: str = "",
    component: str = DEFAULT_COMPONENT,
    secrets: Sequence[str] = (),
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    retention_days: int = DEFAULT_LOG_RETENTION_DAYS,
    total_bytes: int = DEFAULT_LOG_TOTAL_BYTES,
) -> None:
    """Configure unified logging with consistent format.

    Args:
        level: Log level (DEBUG, INFO, WARN, ERROR) applied to every handler.
        log_file: Path to `agent.log`. If empty, logs to stderr only and no
            files are created. `task.log` and `error.log` are written beside
            it -- the three belong to one directory, so naming one names all
            three.
        component: Component name for log prefix.
        secrets: Values this process holds that must never be written. They are
            masked verbatim in every line, whatever shape they appear in.
        max_bytes: Cap for a single file, and for a single record inside it.
        retention_days: How many days of rolled files survive.
        total_bytes: Cap for the three files together.
    """
    handlers: dict[str, object] = {
        "stderr": {
            "class": "logging.StreamHandler",
            "stream": sys.stderr,
            "formatter": "default",
        },
    }

    if log_file:
        agent_path = Path(log_file).expanduser()
        directory = agent_path.parent
        budget = LogBudget(
            directory, total_bytes=total_bytes, retention_days=retention_days
        )
        # Declared before the prune, not by the handlers after it: the prune
        # matches file names against the registered families, so an unregistered
        # budget deletes nothing. A run in which something rolls later would
        # still look as though startup cleanup worked.
        for path in (agent_path, directory / TASK_LOG_NAME, directory / ERROR_LOG_NAME):
            budget.register(path)
        # Before the first write, not only on a roll: history that aged out while
        # the Agent was stopped has to go even if nothing rolls today.
        budget.prune()

        # The ruling's policy for all three (六): one size cap per file, one
        # window, one budget -- and the same budget object, which is what makes
        # 400 MB the Agent's footprint rather than each file's. `agent.log` keeps
        # the caller's path; the others are siblings.
        def file_handler(path: Path, **extra: object) -> dict[str, object]:
            return {
                "()": BoundedFileHandler,
                "path": str(path),
                "max_bytes": max_bytes,
                "budget": budget,
                "formatter": "default",
                **extra,
            }

        handlers["agent"] = file_handler(agent_path)
        handlers["task"] = file_handler(
            directory / TASK_LOG_NAME, filters=["task_only"]
        )
        handlers["error"] = file_handler(
            directory / ERROR_LOG_NAME,
            filters=["errors_only"],
            formatter="error_record",
        )

    config: dict[str, object] = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": RedactingFormatter,
                "format": FMT,
                "datefmt": DATEFMT,
                "secrets": list(secrets),
            },
            # `error.log` only: the ruling's fields are rendered here, and this
            # is also the handler that must not print a traceback.
            "error_record": {
                "()": ErrorRecordFormatter,
                "format": ERROR_FMT,
                "datefmt": DATEFMT,
                "secrets": list(secrets),
            },
        },
        "filters": {
            "task_only": {"()": TaskLogFilter},
            "errors_only": {"()": ErrorLogFilter},
        },
        "handlers": handlers,
        "root": {
            "level": level.upper(),
            "handlers": list(handlers.keys()),
        },
        "loggers": {
            component: {
                "level": level.upper(),
                "propagate": False,
                "handlers": list(handlers.keys()),
            },
        },
    }

    logging.config.dictConfig(config)  # type: ignore[arg-type]


def _prepare_log_dir(log_file: str) -> bool:
    """Create the log file's directory. False if it cannot be created."""
    try:
        Path(log_file).expanduser().parent.mkdir(parents=True, exist_ok=True)
        return True
    except OSError:
        return False


def configure_from(config: AgentConfig) -> None:
    """Configure logging from resolved configuration.

    A log file that cannot be written degrades to stderr rather than raising:
    the Agent must still start and be able to say why it cannot write its log
    (the `RuntimePaths.ensure` rule, applied to one file). Two ways it can fail,
    and both end the same way: the directory cannot be created, or the directory
    is fine but the file itself cannot be opened -- an unwritable path, or one
    that is a directory.
    """
    log_file = config.log_file
    # The runtime token is the one credential this process holds in memory and
    # is most likely to be printed by accident (it is passed around as a string).
    secrets = [config.runtime_token] if config.runtime_token else []
    if log_file and not _prepare_log_dir(log_file):
        logging.getLogger(__name__).warning(
            "cannot create the directory for %s; logging to stderr only", log_file
        )
        log_file = ""
    try:
        configure_logging(
            level=config.log_level,
            log_file=log_file,
            secrets=secrets,
            max_bytes=config.log_max_bytes,
            retention_days=config.log_retention_days,
            total_bytes=config.log_total_bytes,
        )
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "cannot write the log file %s (%s); logging to stderr only",
            log_file,
            type(exc).__name__,
        )
        configure_logging(
            level=config.log_level, log_file="", secrets=secrets
        )
