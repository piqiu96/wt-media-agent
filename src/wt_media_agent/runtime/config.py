"""Agent configuration: environment > file > default.

This is the only module in `src/` permitted to read environment variables
(CHG-056 T-03; the AST test added in T-05 enforces it). Everything else receives
an `AgentConfig` by construction, so the set of things an operator can change
without editing code is exactly the `_SPEC` table below.

Three layers, resolved per key through one code path:

1. the first environment variable in `Field.env` that is present;
2. `<config_dir>/agent.toml`;
3. the built-in default.

Credentials are environment-only. A sensitive key appearing in the file is
ignored and reported by name -- never by value -- because the file travels in
the release artifact and the environment does not (ADR-0016 §7).
"""

from __future__ import annotations

import logging
import os
import ssl
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, MutableMapping

from wt_media_agent.runtime.constants import (
    DEFAULT_BITBROWSER_API_URL,
    DEFAULT_BITBROWSER_TIMEOUT,
    DEFAULT_CLOUD_BASE_URL,
    DEFAULT_LOCAL_API_HOST,
    DEFAULT_LOCAL_API_PORT,
    DEFAULT_LOG_MAX_RECORD_BYTES,
    DEFAULT_LOG_RETENTION_DAYS,
)
from wt_media_agent.runtime.paths import RuntimePaths, is_frozen, repository_root

logger = logging.getLogger(__name__)

CONFIG_DIR_NAME = "config"
CONFIG_FILE_NAME = "agent.toml"


def configure_frozen_ca_bundle(
    *,
    frozen: bool | None = None,
    bundle_root: Path | None = None,
    environment: MutableMapping[str, str] | None = None,
) -> Path | None:
    """Select the bundled public CA store before a frozen Agent opens HTTPS.

    An explicit SSL_CERT_FILE supports a private trust environment. Source runs
    retain Python's existing system trust selection. A frozen release with no
    usable CA store fails at startup instead of silently losing task polls.
    """
    if not (is_frozen() if frozen is None else frozen):
        return None
    values = os.environ if environment is None else environment
    explicit = values.get("SSL_CERT_FILE", "").strip()
    embedded_root = getattr(sys, "_MEIPASS", None)
    if not explicit and bundle_root is None and not embedded_root:
        raise RuntimeError("CA bundle location is missing from the frozen Agent")
    root = bundle_root if bundle_root is not None else Path(embedded_root or "")
    selected = Path(explicit).expanduser() if explicit else root / "certs" / "ca-bundle.pem"
    try:
        ssl.create_default_context(cafile=str(selected))
    except (OSError, ssl.SSLError) as exc:
        raise RuntimeError(f"CA bundle is missing or invalid: {selected}") from exc
    if not explicit:
        values["SSL_CERT_FILE"] = str(selected)
    logger.info("TLS CA bundle ready source=%s", "override" if explicit else "bundled")
    return selected

#: Leaf key names that must never be honoured from a file, matched case-
#: insensitively and by exact leaf name. Deliberately broad: a credential that
#: lands in a shipped file is a leak whether or not this module has a use for it.
SENSITIVE_KEY_NAMES = frozenset(
    {
        "token",
        "runtime_token",
        "auth_token",
        "password",
        "passwd",
        "secret",
        "client_secret",
        "authorization",
        "bearer",
        "refresh",
        "refresh_token",
        "cookie",
        "cookies",
        "api_key",
        "apikey",
        "private_key",
    }
)

_MISSING = object()


class ConfigError(ValueError):
    """A configured value is present but cannot be interpreted."""


def _as_str(raw: object) -> str:
    return raw if isinstance(raw, str) else str(raw)


def _as_int(raw: object) -> int:
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"expected an integer, got {raw!r}") from exc


def _as_float(raw: object) -> float:
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"expected a number, got {raw!r}") from exc


def _as_bool(raw: object) -> bool:
    """Accept the spellings a shell script or a TOML file actually produces.

    An unrecognised spelling raises rather than defaulting: "run the runner"
    is the switch that decides whether this process starts polling Cloud, and
    a typo must not silently pick either answer.
    """
    if isinstance(raw, bool):
        return raw
    text = _as_str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"", "0", "false", "no", "off"}:
        return False
    raise ConfigError(f"expected a boolean, got {raw!r}")


def _as_log_level(raw: object) -> str:
    """Return a level name the `logging` module will accept.

    `logging.config.dictConfig` raises a bare `ValueError: Unknown level` on a
    typo, naming neither the file nor the key. Failing here instead keeps every
    rejected value reported the same way -- by key path.
    """
    name = _as_str(raw).upper()
    if name not in logging.getLevelNamesMapping():
        raise ConfigError(f"expected a log level name, got {raw!r}")
    return name


def _as_positive_int(raw: object) -> int:
    """A size or a window, in bytes or days. Zero and below are rejected.

    Zero reads like "no limit" and means the opposite for both of the log keys,
    which is why it is refused rather than clamped: a record of no bytes cannot
    be written at all, and a window of no days deletes every archive at the next
    roll. Failing here reports the key path; accepting it would turn a typo into
    either a log that loses its history or one that is never trimmed.
    """
    value = _as_int(raw)
    if value <= 0:
        raise ConfigError(f"expected a positive integer, got {raw!r}")
    return value


def _as_optional_float(raw: object) -> float | None:
    """Optional override: unset or unparseable both mean "not configured".

    This mirrors the behaviour these two keys had before CHG-056, when
    `clients/bitbrowser` read them itself and fell back to the client's default
    on a ValueError. Raising here would turn a previously-working typo into a
    startup failure, which is a behaviour change this task is not making.
    """
    if raw is None:
        return None
    if isinstance(raw, str) and not raw.strip():
        return None
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Field:
    """One configuration key: where it comes from and how it is interpreted."""

    name: str  # attribute on AgentConfig
    path: str  # dotted path inside the TOML document
    env: tuple[str, ...]  # environment names, canonical first, aliases after
    default: object
    coerce: Callable[[object], object]
    secret: bool = False  # env-only; never read from a file


#: The complete set of things an operator can configure. Order is documentation.
_SPEC: tuple[Field, ...] = (
    Field("environment", "environment", ("WT_MEDIA_ENV",), "development", _as_str),
    Field("cloud_base_url", "cloud.base_url", ("WT_MEDIA_CLOUD_BASE_URL",), DEFAULT_CLOUD_BASE_URL, _as_str),
    Field("cloud_timeout_seconds", "cloud.timeout_seconds", ("WT_MEDIA_CLOUD_TIMEOUT",), 10.0, _as_float),
    Field("local_api_host", "local_api.host", ("WT_MEDIA_LOCAL_API_HOST",), DEFAULT_LOCAL_API_HOST, _as_str),
    Field("local_api_port", "local_api.port", ("WT_MEDIA_LOCAL_API_PORT",), DEFAULT_LOCAL_API_PORT, _as_int),
    Field("bitbrowser_api_url", "bitbrowser.api_url", ("WT_MEDIA_BITBROWSER_API_URL",), DEFAULT_BITBROWSER_API_URL, _as_str),
    Field("bitbrowser_timeout_seconds", "bitbrowser.timeout_seconds", ("WT_MEDIA_BITBROWSER_TIMEOUT_SECONDS",), DEFAULT_BITBROWSER_TIMEOUT, _as_float),
    Field("bitbrowser_create_timeout_seconds", "bitbrowser.create_timeout_seconds", ("WT_MEDIA_BITBROWSER_CREATE_TIMEOUT_SECONDS",), None, _as_optional_float),
    Field("bitbrowser_mutation_timeout_seconds", "bitbrowser.mutation_timeout_seconds", ("WT_MEDIA_BITBROWSER_MUTATION_TIMEOUT_SECONDS",), None, _as_optional_float),
    Field("agent_id", "agent.id", ("WT_MEDIA_AGENT_ID",), "local-agent-dev", _as_str),
    Field("data_dir", "agent.data_dir", ("WT_MEDIA_AGENT_DATA_DIR",), "", _as_str),
    # The switch that decides whether this process polls Cloud for tasks. Off by
    # default: an Agent started by hand, or by a health check, must not begin
    # claiming tasks just because it was started. Desktop and the acceptance
    # scripts turn it on explicitly.
    Field("run_runner", "agent.run_runner", ("WT_MEDIA_AGENT_RUN_RUNNER",), False, _as_bool),
    # WT_MEDIA_AGENT_LOG_LEVEL is a compatibility alias: it is what
    # local_api/server.py read before T-03, and what
    # wt-media-workspace/scripts/m2b_local_acceptance.py still exports.
    # WT_MEDIA_AGENT_LOG_FILE has no alias deliberately -- it is the health
    # scripts' own variable for the file they redirect stdout/stderr into
    # (scripts/README.md), and no Agent code ever read it. Aliasing it would
    # make the Agent install a second handler on the very file its caller is
    # already redirecting into.
    Field("log_level", "logging.level", ("WT_MEDIA_LOG_LEVEL", "WT_MEDIA_AGENT_LOG_LEVEL"), "INFO", _as_log_level),
    Field("log_file", "logging.file", ("WT_MEDIA_LOG_FILE",), "", _as_str),
    # Log retention (the rulings 六 and 三). Both are overridable because a
    # deployment may want a shorter window than the default; the shipped numbers
    # live in `runtime/constants.py` and are asserted by the tests.
    #
    # There is deliberately no `max_bytes` and no `total_bytes` any more
    # (CHG-058 T-02): the user's 2026-09-24 ruling bounds how long history stays,
    # not how much of it there is.
    Field("log_max_record_bytes", "logging.max_record_bytes", ("WT_MEDIA_LOG_MAX_RECORD_BYTES",), DEFAULT_LOG_MAX_RECORD_BYTES, _as_positive_int),
    Field("log_retention_days", "logging.retention_days", ("WT_MEDIA_LOG_RETENTION_DAYS",), DEFAULT_LOG_RETENTION_DAYS, _as_positive_int),
    # Environment-only. A value in the TOML is ignored by construction.
    Field("runtime_token", "runtime_token", ("WT_MEDIA_AGENT_RUNTIME_TOKEN",), "", _as_str, secret=True),
)


@dataclass(frozen=True)
class AgentConfig:
    """Resolved configuration. Immutable; `paths` is derived, not configured."""

    environment: str
    cloud_base_url: str
    cloud_timeout_seconds: float
    local_api_host: str
    local_api_port: int
    bitbrowser_api_url: str
    bitbrowser_timeout_seconds: float
    bitbrowser_create_timeout_seconds: float | None
    bitbrowser_mutation_timeout_seconds: float | None
    agent_id: str
    data_dir: str
    run_runner: bool
    log_level: str
    log_file: str
    log_max_record_bytes: int
    log_retention_days: int
    #: `repr=False`: the ruling 十 forbids a sensitive object printing itself,
    #: and a dataclass repr is what a debugger, a traceback and `print` all use.
    #: Excluding the field is one word that cannot drift; a hand-written
    #: `__repr__` would have to be kept in step with every field added here.
    runtime_token: str = field(repr=False)
    #: Derived from `environment` and `data_dir`, not configured directly.
    paths: RuntimePaths

    @property
    def local_api_address(self) -> str:
        return f"{self.local_api_host}:{self.local_api_port}"

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() == "production"


#: The directory a macOS `.app` keeps its sealed resources in. The packaged
#: branch below looks for `config/` there -- one level up from `Contents/MacOS`,
#: where the executable sits -- because that is where shipping stages the Agent's
#: configuration (`wt-media-desktop/scripts/stage-release-config.sh`).
BUNDLE_RESOURCES_DIR_NAME = "Resources"


def _bundled_config_dir(exe: Path) -> Path | None:
    """The config directory a packaged Agent was shipped with, or `None`.

    Derived from `exe` and never from an environment variable: ADR-0016 §6
    forbids a switch between the checkout's `config/` and the shipped one, so the
    only thing allowed to decide is where the process is running from.

    Two shapes, in this order: the `.app` layout Tauri produces (resources sealed
    under `Contents/Resources`), then a plain `config/` beside the executable,
    which is what an unpacked distribution would carry.
    """
    directory = exe.resolve().parent
    candidates = (
        directory.parent / BUNDLE_RESOURCES_DIR_NAME / CONFIG_DIR_NAME,
        directory / CONFIG_DIR_NAME,
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    logger.warning(
        "no packaged config directory found (looked at %s); using the built-in defaults",
        " and ".join(str(candidate) for candidate in candidates),
    )
    return None


def default_config_dir(*, frozen: bool | None = None, exe: Path | None = None) -> Path:
    """The config directory this process reads.

    A packaged Agent reads what shipping put beside it -- `Contents/Resources/
    config` in a macOS `.app`, `config/` next to the executable otherwise -- and a
    checkout reads the repository's own `config/`. (That is the same dev/installed
    split `RuntimePaths.resolve` makes, argued in ADR-0016 §5-§6.)

    `frozen` and `exe` are the seams that let the packaged branch be tested
    without building a bundle; both default to this process's own values. A
    packaged Agent that matches neither shape falls back to the checkout path --
    which inside a bundle is an extraction directory that does not exist, i.e. the
    built-in defaults -- but says so at WARNING first, because "the release
    shipped no configuration" is exactly the regression a quiet fallback hides.
    """
    frozen = is_frozen() if frozen is None else frozen
    if frozen:
        bundled = _bundled_config_dir(exe if exe is not None else Path(sys.executable))
        if bundled is not None:
            return bundled
    return repository_root() / CONFIG_DIR_NAME


def _dig(document: Mapping[str, object], path: str) -> object:
    node: object = document
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _leaves(node: Mapping[str, object], prefix: str = "") -> list[tuple[str, str]]:
    """Flatten to `(dotted_path, leaf_name)` pairs for keys with scalar values."""
    found: list[tuple[str, str]] = []
    for key, value in node.items():
        path = f"{prefix}{key}"
        if isinstance(value, Mapping):
            found.extend(_leaves(value, f"{path}."))
        else:
            found.append((path, key))
    return found


def _read_document(
    config_dir: Path | None, *, frozen: bool | None = None, exe: Path | None = None
) -> Mapping[str, object]:
    """Parse `<config_dir>/agent.toml`, resolving the directory when not given.

    Missing is not an error -- it means "use the defaults". Malformed is an
    error: a broken file shipping in a release should be loud, not silently
    reinterpreted as an unconfigured agent.

    `frozen`/`exe` are passed through to `default_config_dir` so that a caller
    asking `load_config` to stand in for a packaged Agent gets the packaged
    directory too, instead of this process's own.
    """
    directory = (
        config_dir if config_dir is not None else default_config_dir(frozen=frozen, exe=exe)
    )
    path = directory / CONFIG_FILE_NAME
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        logger.debug("no config file at %s; using defaults", path)
        return {}
    except OSError as exc:
        logger.warning("cannot read %s (%s); using defaults", path, type(exc).__name__)
        return {}
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


def _warn_about_ignored_keys(document: Mapping[str, object], known: frozenset[str]) -> None:
    """Report sensitive and unknown keys by name. Values are never logged."""
    for path, leaf in _leaves(document):
        if leaf.lower() in SENSITIVE_KEY_NAMES:
            logger.warning(
                "ignoring sensitive key %s from the config file; credentials are "
                "environment-only",
                path,
            )
        elif path not in known:
            logger.warning("ignoring unknown config key %s", path)


def load_config(
    *,
    config_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    frozen: bool | None = None,
    home: Path | None = None,
    repo_root: Path | None = None,
    exe: Path | None = None,
) -> AgentConfig:
    """Resolve the configuration. Reads the filesystem and the environment."""
    environment = os.environ if env is None else env
    document = _read_document(config_dir, frozen=frozen, exe=exe)
    _warn_about_ignored_keys(document, frozenset(field.path for field in _SPEC))

    values: dict[str, object] = {}
    for spec in _SPEC:
        raw: object = _MISSING
        for name in spec.env:
            if name in environment:
                raw = environment[name]
                break
        if raw is _MISSING and not spec.secret:
            raw = _dig(document, spec.path)
        if raw is _MISSING:
            raw = spec.default
        try:
            values[spec.name] = spec.coerce(raw)
        except ConfigError as exc:
            raise ConfigError(f"{spec.path}: {exc}") from exc

    # The cross-check that lived here -- a total budget below one file's cap is a
    # contradiction -- has nothing left to compare (CHG-058 T-02): both numbers
    # are gone, and `log_max_record_bytes` is a per-line bound that no other key
    # constrains.

    paths = RuntimePaths.resolve(
        data_dir=str(values["data_dir"]),
        environment=str(values["environment"]),
        frozen=frozen,
        home=home,
        repo_root=repo_root,
    )
    if not values["log_file"]:
        values["log_file"] = paths.default_log_file

    return AgentConfig(**values, paths=paths)  # type: ignore[arg-type]


_cached: AgentConfig | None = None


def get_config() -> AgentConfig:
    """Process-wide configuration, loaded on first use.

    This exists so modules outside `runtime/` never touch `os.environ` between
    this task and T-04. It is a cache, not a registry or a container: T-04's
    bootstrap loads the configuration once and injects it, and these call sites
    disappear.
    """
    global _cached
    if _cached is None:
        _cached = load_config()
    return _cached


def reset_config_cache() -> None:
    """Drop the cache so the next `get_config()` reloads. For tests."""
    global _cached
    _cached = None
