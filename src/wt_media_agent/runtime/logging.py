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
import sys
from pathlib import Path

from wt_media_agent.runtime.config import AgentConfig

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


class NoTracebackFormatter(logging.Formatter):
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

    def format(self, record: logging.LogRecord) -> str:
        return super().format(self.isolate(record))


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

    def format(self, record: logging.LogRecord) -> str:
        isolated = self.isolate(record)
        isolated.error_code = _field(record, ERROR_CODE_FIELD) or NO_VALUE
        isolated.extra_fields = "".join(
            f" {name}={_field(record, name)}"
            for name in (TASK_ID_FIELD, CONTEXT_FIELD)
            if _field(record, name)
        )
        # The base *class*, not `super()`: `NoTracebackFormatter.format` would
        # isolate the record a second time for no reason.
        return logging.Formatter.format(self, isolated)


def configure_logging(
    level: str = "INFO",
    log_file: str = "",
    component: str = DEFAULT_COMPONENT,
) -> None:
    """Configure unified logging with consistent format.

    Args:
        level: Log level (DEBUG, INFO, WARN, ERROR) applied to every handler.
        log_file: Path to `agent.log`. If empty, logs to stderr only and no
            files are created. `task.log` and `error.log` are written beside
            it -- the three belong to one directory, so naming one names all
            three.
        component: Component name for log prefix.
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
                "format": FMT,
                "datefmt": DATEFMT,
            },
            # `error.log` only: the ruling's fields are rendered here, and this
            # is also the handler that must not print a traceback.
            "error_record": {
                "()": ErrorRecordFormatter,
                "format": ERROR_FMT,
                "datefmt": DATEFMT,
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
    if log_file and not _prepare_log_dir(log_file):
        logging.getLogger(__name__).warning(
            "cannot create the directory for %s; logging to stderr only", log_file
        )
        log_file = ""
    configure_logging(level=config.log_level, log_file=log_file)
