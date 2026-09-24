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

Rolled hourly, kept by age (CHG-058 T-02; the rulings 三 and 六): the live file
is `agent.log` and each hour that ends is renamed to `agent.log.<YYYY-MM-DD-HH>`
-- the shape the Desktop writes, so one directory listing teaches the same shape
twice. A single oversized record is still truncated and marked; there is no
longer any cap on a file's size or on the three files together (the ruling 三:
bound how long history stays, not how much of it there is).

The rotation and the naming are the **stdlib's** (`TimedRotatingFileHandler`);
the age rule is ours, because the stdlib only offers deletion by count. That
asymmetry against the Desktop -- which gets all three from `file-rotate` -- is
deliberate and is written down in the programme baseline rather than left for a
reader to notice.

Moved here from `wt_media_agent/log_setup.py` (CHG-056 T-03): log setup is
runtime-layer work (ADR-0016 §4).
"""

from __future__ import annotations

import contextvars
import logging
import logging.config
import logging.handlers
import re
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta
from pathlib import Path

from wt_media_agent.runtime.config import SENSITIVE_KEY_NAMES, AgentConfig
from wt_media_agent.runtime.constants import (
    DEFAULT_LOG_MAX_RECORD_BYTES,
    DEFAULT_LOG_RETENTION_DAYS,
)

#: Matches the package the modules actually log under (`getLogger(__name__)`
#: produces `wt_media_agent.<module>`). The pre-T-03 default was the hyphenated
#: `wt-media-agent`, which matches no logger in the codebase -- harmless only
#: because the function never ran.
DEFAULT_COMPONENT = "wt_media_agent"

AGENT_LOG_NAME = "agent.log"
TASK_LOG_NAME = "task.log"
ERROR_LOG_NAME = "error.log"

#: How an archive names the hour it holds: `agent.log.2026-09-24-20`. Zero
#: padded and fixed width on purpose -- comparing two of these as strings
#: compares the hours they name, which is what the retention rule does (and what
#: `file-rotate` does on the Desktop side, so both sides agree at the boundary).
ARCHIVE_FORMAT = "%Y-%m-%d-%H"

#: What a truncated record carries, byte for byte the Desktop's marker so one
#: reader learns one spelling.
TRUNCATION_MARKER = " truncate=true original_size="

#: Where the task narrative comes from. Matched on a dotted boundary, so a
#: future `wt_media_agent.runner_pool` is a different component and stays out.
TASK_LOGGER_PREFIX = "wt_media_agent.runner"

#: Every record routed into the three files. `stderr` deliberately has none:
#: the terminal must show what the files show, filters and all.
#:
#: `%(operation_field)s` renders as the empty string outside a request (T-21),
#: so the line a request never touched is byte-for-byte what it was before the
#: field existed. It is a *render slot*, not a record attribute: the name is
#: deliberately not `operation_id`, so a caller who passes
#: `extra={"operation_id": ...}` cannot have it silently overwritten here.
FMT = "%(asctime)s [%(levelname)s] %(name)s:%(operation_field)s %(message)s"
DATEFMT = "%Y-%m-%dT%H:%M:%S"

#: The ruling's fields for `error.log` (三). They are carried by `extra={...}`
#: on the logging call and defaulted by the formatter. `error_code` is required
#: there, so an event that has no code says so rather than dropping the field;
#: `task_id` and `context` are optional and are left out when unset.
ERROR_CODE_FIELD = "error_code"
TASK_ID_FIELD = "task_id"
CONTEXT_FIELD = "context"

#: The request-scoped id (T-21). Delivered inside this process only: D-10 keeps
#: it off the wire, so nothing here is a header, a query parameter or a sidecar
#: environment variable -- and the tests assert the responses do not carry it.
OPERATION_ID_FIELD = "operation_id"

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
#:
#: `%(operation_field)s` takes the same place it takes in `FMT` -- right after the
#: logger name, before `error_code` -- so the field a reader looks for sits at one
#: offset in all three files. D-03's five fields are all still here, in order, with
#: the message last.
ERROR_FMT = (
    f"%(asctime)s [%(levelname)s] %(name)s:%(operation_field)s"
    f" {ERROR_CODE_FIELD}=%(error_code)s%(extra_fields)s %(message)s"
)


#: The id of the request this thread is serving, if it is serving one (T-21).
#:
#: A `ContextVar` and not a thread-local: `ThreadingHTTPServer` gives each
#: connection a thread, so a thread-local would happen to work here -- and would
#: stop working the moment any part of a request moved to an executor. Setting it
#: is scoped to the context, and `reset(token)` *restores* the previous value
#: rather than clearing it, so a request nested inside another hands the outer id
#: back when it ends.
_OPERATION_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "wt_media_agent_operation_id", default=""
)


def begin_operation(value: str) -> contextvars.Token[str]:
    """Make `value` this request's id. Pair it with `end_operation` in a `finally`.

    The value comes from the caller -- `local_api.server` generates it with
    `secrets.token_hex(8)`, so an id is 16 hexadecimal characters -- rather than
    from here: this module owns where the field goes in a line, and the server owns
    what counts as one request.
    """
    return _OPERATION_ID.set(value)


def end_operation(token: contextvars.Token[str]) -> None:
    """Undo one `begin_operation`, restoring whatever id was in scope before it."""
    _OPERATION_ID.reset(token)


def _operation_field() -> str:
    """The id as a format field, or "" when no request is in scope.

    Empty is the whole contract for a record written outside a request: the line
    keeps exactly the shape it had before this field existed. The value is escaped
    like any other field -- an id is generated here today, but a format field that
    can carry a newline forges records in a file whose contract is one per line.
    """
    value = _OPERATION_ID.get()
    if not value:
        return ""
    return f" {OPERATION_ID_FIELD}={_single_line(value)}"


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

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        """The record as this line renders it. Subclass hook.

        The request's id is stamped here, as a *render slot*, rather than carried
        on the record by whoever logged it -- the same conclusion T-05 reached for
        `error_code`: a field that is part of the line's shape belongs to the thing
        that shapes the line, and all three handlers render through this one place.
        """
        record.operation_field = _operation_field()
        return record

    def format(self, record: logging.LogRecord) -> str:
        """Render one record, then mask what the rendering produced.

        The base *class* call for the rendering, so the record is prepared exactly
        once: `super().format` would reach the same `logging.Formatter.format`, but
        going through `self.prepare` is what lets a subclass prepare its own way.
        Masking after rendering is T-06's rule -- by then the message and the
        traceback are both text, which is the point, since a traceback is where a
        credential turns up.
        """
        rendered = logging.Formatter.format(self, self.prepare(record))
        return redact(rendered, self.secrets)


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
        return super().prepare(self.isolate(record))


def _single_line(value: object) -> str:
    """A field's value as one line of text.

    Newlines are escaped, not stripped: `task_id` arrives from Cloud and is
    interpolated verbatim, so a value carrying a newline would otherwise forge a
    record in a file whose whole contract is one line per record. Values are
    expected to be tokens; T-06 owns redacting anything sensitive inside them.
    """
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


def _field(record: logging.LogRecord, name: str) -> str:
    """One field's value as a single line of text, or "" if the record has none."""
    value = getattr(record, name, "")
    if not value:
        return ""
    return _single_line(value)


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


class LogRetention:
    """How long the rolled files of the three log files stay.

    The window, and nothing else: the ruling 三 cancelled the volume bounds (a
    single file's cap and the three files' shared budget) and kept the age rule.
    So this object answers one question -- which files are past the window -- and
    `prune` is the only way it changes anything on disk. `HourlyFileHandler`
    supplies the rotation and the naming; this supplies the deletion the stdlib
    does not have.

    It deletes rolled files only. The file a handler is currently writing is
    never a candidate (the ruling 六: 当前打开的文件永不被删) -- a rolled file is
    history, the live one is the only copy of what just happened. With this
    naming rule that is nearly unreachable, a stamped name cannot equal a live
    one, but not entirely unreachable: `logging.file` lets an operator name the
    live file anything, including something that looks like an archive.

    The clock is injected, so "the window closed" is a statement a test makes
    rather than something it waits for.
    """

    def __init__(
        self,
        directory: Path,
        *,
        retention_days: int = DEFAULT_LOG_RETENTION_DAYS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.directory = Path(directory)
        self.retention_days = retention_days
        self.clock = clock
        self._live: set[Path] = set()
        self._families: list[re.Pattern[str]] = []

    def cutoff(self) -> str:
        """The oldest stamp still inside the window.

        Local time, in the same `ARCHIVE_FORMAT` the archives carry, because the
        two are compared as strings: fixed-width zero-padded fields make that
        comparison chronological. This is the Desktop's rule to the letter -- the
        crate formats its cutoff the same way and compares with the same `<`, so
        "inside the window" means the same hour on both sides.
        """
        edge = datetime.fromtimestamp(self.clock()) - timedelta(days=self.retention_days)
        return edge.strftime(ARCHIVE_FORMAT)

    def register(self, live: Path) -> None:
        """Declare one file that is currently open, and so never prunable.

        This is also what teaches the object its family names: pruning matches
        file names against the patterns registered here, so a retention object
        nobody registered deletes nothing at all -- while looking exactly like one
        that found nothing worth deleting (measured in CHG-057 T-07: with the
        counting rule that preceded this, a real run left its stale files in place
        for its whole lifetime). `configure_logging` therefore declares all three
        before the first prune, and each handler declares its own again when it is
        built.
        """
        if live in self._live:
            return
        self._live.add(live)
        # `agent.log` -> `agent.log.2026-09-24-20`: the live name, a dot, the
        # hour. Built from the whole name rather than from stem and suffix,
        # because `Path("agent.log").stem` is `agent` -- the dot is part of it.
        self._families.append(
            re.compile(rf"^{re.escape(live.name)}\.(\d{{4}}-\d{{2}}-\d{{2}}-\d{{2}})$")
        )

    def rolled(self) -> list[tuple[Path, str]]:
        """The rolled files, each with the stamp it carries, oldest first."""
        found: list[tuple[Path, str]] = []
        for path in self.directory.iterdir():
            if path in self._live or not path.is_file():
                continue
            for pattern in self._families:
                match = pattern.match(path.name)
                if match:
                    found.append((path, match.group(1)))
                    break
        # By stamp, which is also by time: the fields are fixed width and zero
        # padded. No index tie-break is needed, because the archive name carries
        # the hour and nothing else, so two archives cannot share a name.
        found.sort(key=lambda item: item[1])
        return found

    def prune(self) -> list[Path]:
        """Delete what is out of the window. Returns what it deleted.

        Strictly older: a file stamped exactly at the cutoff hour is kept, which
        is the boundary the crate's `FileLimit::Age` draws on the Desktop side.
        """
        deleted: list[Path] = []
        cutoff = self.cutoff()
        for path, stamp in self.rolled():
            if stamp < cutoff and self._remove(path):
                deleted.append(path)
        return deleted

    def _remove(self, path: Path) -> bool:
        try:
            path.unlink()
        except OSError as exc:
            # Not fatal to the process, but not quiet either: if the deletion
            # never succeeds the history has no bound left, and a reader
            # wondering why the directory is full needs this line.
            _report(f"cannot delete {path} ({type(exc).__name__}: {exc})")
            return False
        return True


class HourlyFileHandler(logging.handlers.TimedRotatingFileHandler):
    """One Agent log file: hourly rolls, an age-only window, one-line records.

    The stdlib supplies the two things this side of the ruling is about -- the
    hourly trigger and the rename to `<live>.<YYYY-MM-DD-HH>` -- so what is left
    for this subclass is the two the stdlib does not do:

    * `backupCount=0` turns off its own deletion, which is by *count*. The window
      here is by age, and two deleters would fight over the same directory:
      `LogRetention.prune` is the only one that removes anything.
    * `format` truncates an oversized record and marks it. This is the same
      arithmetic the Desktop's `fit` does, over the same marker, so one configured
      cap truncates a line at the same length on both sides.

    Local time throughout (`utc=False`), matching the record stamp: the stdlib
    formatter renders `asctime` with `time.localtime` and no converter is
    installed, so a file's name and the lines inside it name the same hour
    (CHG-058 §6 D-09 -- the Desktop was changed to match this side).

    What the stdlib does that a reader should know about: when an archive for the
    hour already exists, `doRollover` **returns early** ("Already rolled over")
    and skips the roll -- and it returns *before* advancing `rolloverAt`, so every
    later write computes the same name and skips again. The live file stays
    unrolled for the life of the process and grows past any window. The Desktop's
    crate does the opposite: the same-name archive gets a `.1` appended, and its
    age rule reads only the timestamp part, so the extra file is still pruned
    (`file-rotate` 0.8.0 `suffix.rs`, `rotate_file` + `too_old`).

    Reaching the stdlib branch needs two writers on one live file, which is why
    both sides now refuse a second instance (Desktop) or a second bind (Agent).
    It is registered as a boundary rather than fixed here: giving the archive a
    distinct name would be the crate's cascade, and that name would not match this
    side's four-field retention pattern, so the file would outlive the window.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_record_bytes: int = DEFAULT_LOG_MAX_RECORD_BYTES,
        retention: LogRetention,
        encoding: str = "utf-8",
    ) -> None:
        self.base_path = Path(path)
        # The directory is ours to create: `FileHandler` opens the file and does
        # not make its parents. `configure_from` creates it too, so an operator
        # who configured a path by hand gets the same treatment as one who left
        # the key empty.
        self.base_path.parent.mkdir(parents=True, exist_ok=True)
        # At least one byte, so the arithmetic below cannot be handed a limit of
        # zero by a caller that skipped the configuration layer's validation.
        self.max_record_bytes = max(1, max_record_bytes)
        super().__init__(
            str(self.base_path),
            when="H",
            interval=1,
            # Neither count nor size: the window is `LogRetention`'s.
            backupCount=0,
            encoding=encoding,
            # Opened eagerly, like `FileHandler` without `delay`: the Agent must
            # not be able to start and look as though it has no log configured.
            delay=False,
            utc=False,
        )
        # `when` chooses a suffix of its own -- `%Y-%m-%d_%H` for hourly, with an
        # underscore -- and `__init__` takes no `suffix` argument, so the shape the
        # ruling asks for has to be set afterwards. Measured, not assumed: the
        # first version of this class produced `agent.log.2026-09-24_20`.
        self.suffix = ARCHIVE_FORMAT
        self.retention = retention
        retention.register(self.base_path)

    def computeRollover(self, currentTime: float) -> int:
        """The next *hour boundary*, not an hour from this write.

        The stdlib has an alignment branch only for `MIDNIGHT` and the weekly
        forms; for `when="H"` it returns `currentTime + interval`, so the first
        roll is an hour after the process started and every later one an hour
        after the previous roll. That matters because `doRollover` names the
        archive from `rolloverAt - interval`: unaligned, a file named for hour 20
        holds a window that *starts* in hour 20 and runs 58 minutes into hour 21,
        so its name and its contents disagree -- and the Desktop, rolling on the
        clock hour, would be naming a different window with the same name. Aligned,
        `rolloverAt - interval` is exactly the hour that ended (D-09).

        Verified against the installed stdlib rather than assumed: the probe that
        caught this read `rolloverAt` as `21:58` on a handler built at `20:58`.
        """
        moment = time.localtime(currentTime)
        return int(currentTime) - (moment.tm_min * 60 + moment.tm_sec) + 3600

    def format(self, record: logging.LogRecord) -> str:
        """The record as it will be written, truncated and marked if too long."""
        return self._fit(super().format(record))

    def doRollover(self) -> None:
        super().doRollover()
        # After the roll, not before: the roll has just renamed the previous hour
        # away and reopened the live file, and pruning is a separate decision
        # about names that are already on disk.
        self.retention.prune()

    def _fit(self, text: str) -> str:
        """Truncate to one line's worth, carrying the size the record *had*.

        The cap counts the newline `emit` appends, which is why the limit is one
        less than the configured number. `original_size` is a byte count -- the
        only way a reader can tell a truncated line from a short one.
        """
        raw = text.encode(self.encoding)
        limit = self.max_record_bytes - 1
        if len(raw) <= limit:
            return text
        marker = f"{TRUNCATION_MARKER}{len(raw)}"
        # A cap too small to hold the marker leaves no room for the head. The
        # marker is still written: a record marked truncated against an unusable
        # cap is more use than one with no size information, and the configuration
        # layer rejects caps that small.
        room = max(limit - len(marker.encode(self.encoding)), 0)
        # `errors="ignore"`: the cut is on a byte boundary, so it may land inside
        # a character. Dropping that one character is the point -- emitting half
        # of it would put a replacement byte in a plain-text file.
        head = raw[:room].decode(self.encoding, errors="ignore")
        return head + marker


def configure_logging(
    level: str = "INFO",
    log_file: str = "",
    component: str = DEFAULT_COMPONENT,
    secrets: Sequence[str] = (),
    max_record_bytes: int = DEFAULT_LOG_MAX_RECORD_BYTES,
    retention_days: int = DEFAULT_LOG_RETENTION_DAYS,
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
        max_record_bytes: Cap for a single record, counted with the newline that
            ends it. A record over it is truncated and marked, never rolled.
        retention_days: How many days of rolled files survive.
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
        retention = LogRetention(directory, retention_days=retention_days)
        # Declared before the prune, not by the handlers after it: the prune
        # matches file names against the registered families, so a retention
        # object nobody registered deletes nothing. A run in which something
        # rolls later would still look as though startup cleanup worked.
        for path in (agent_path, directory / TASK_LOG_NAME, directory / ERROR_LOG_NAME):
            retention.register(path)
        # Before the first write, not only on a roll: history that aged out while
        # the Agent was stopped has to go even if nothing rolls today.
        retention.prune()

        # One policy for all three (六): the same window, and one retention
        # object behind the three handlers, so they cannot disagree about what
        # the window is. `agent.log` keeps the caller's path; the others are
        # siblings.
        def file_handler(path: Path, **extra: object) -> dict[str, object]:
            return {
                "()": HourlyFileHandler,
                "path": str(path),
                "max_record_bytes": max_record_bytes,
                "retention": retention,
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
            max_record_bytes=config.log_max_record_bytes,
            retention_days=config.log_retention_days,
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
