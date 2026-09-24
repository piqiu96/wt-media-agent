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

Moved here from `wt_media_agent/log_setup.py` (CHG-056 T-03): log setup is
runtime-layer work (ADR-0016 §4).
"""

from __future__ import annotations

import logging
import logging.config
import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

from wt_media_agent.runtime.config import SENSITIVE_KEY_NAMES, AgentConfig

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

#: The value group is the rest of the line so each key family can decide how far
#: its reach goes: a cookie header's whole tail, an `Authorization` value's whole
#: tail, and for the rest just the leading token.
_KEYED = re.compile(rf"(?i)\b({_KEY})[\"']?(\s*[:=]\s*)([^\n]*)")
_LEADING_VALUE = re.compile(rf"(?:(?:{_BEARER_SCHEMES})\s+)?(?:{_QUOTED_OR_TOKEN})")
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
    return _normalized(key).endswith("cookie")


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


def _mask_keyed(match: re.Match[str]) -> str:
    """Mask one keyed credential, keeping whatever follows it in the line.

    The key and its separator are echoed as they arrived; only the value is
    replaced, and only as far as this key's family reaches.
    """
    key, separator, tail = match.groups()
    if _is_cookie_key(key):
        return f"{key}{separator}{_mask_cookie_pairs(tail)}"
    if _is_opaque_key(key):
        return f"{key}{separator}{REDACTED}"
    leading = _LEADING_VALUE.match(tail)
    if not _is_sensitive_key(key) or leading is None:
        return match.group(0)
    return f"{key}{separator}{_quote_like(leading.group(0))}{tail[leading.end():]}"


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
    text = _KEYED.sub(_mask_keyed, text)
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


def configure_logging(
    level: str = "INFO",
    log_file: str = "",
    component: str = DEFAULT_COMPONENT,
    secrets: Sequence[str] = (),
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
    """
    handlers: dict[str, object] = {
        "stderr": {
            "class": "logging.StreamHandler",
            "stream": sys.stderr,
            "formatter": "default",
        },
    }

    if log_file:
        directory = Path(log_file).expanduser().parent
        # Same rolling policy for all three until T-07 replaces it with the
        # ruling's 20MB / 14 days / total budget (which also caps the single
        # record). `agent.log` keeps the caller's path; the others are siblings.
        def file_handler(path: Path, **extra: object) -> dict[str, object]:
            return {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(path),
                "maxBytes": 10 * 1024 * 1024,  # 10 MB
                "backupCount": 3,
                "formatter": "default",
                **extra,
            }

        handlers["agent"] = file_handler(Path(log_file).expanduser())
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

    A log file whose directory cannot be created degrades to stderr rather than
    raising: the Agent must still start and be able to say why it cannot write
    its log (the `RuntimePaths.ensure` rule, applied to one file).
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
    configure_logging(level=config.log_level, log_file=log_file, secrets=secrets)
