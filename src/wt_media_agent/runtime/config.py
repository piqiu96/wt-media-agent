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
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from wt_media_agent.runtime.constants import (
    DEFAULT_BITBROWSER_API_URL,
    DEFAULT_BITBROWSER_TIMEOUT,
    DEFAULT_CLOUD_BASE_URL,
    DEFAULT_LOCAL_API_HOST,
    DEFAULT_LOCAL_API_PORT,
)
from wt_media_agent.runtime.paths import RuntimePaths, repository_root

logger = logging.getLogger(__name__)

CONFIG_DIR_NAME = "config"
CONFIG_FILE_NAME = "agent.toml"

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
    Field("data_dir", "agent.data_dir", ("WT_MEDIA_AGENT_DATA_DIR",), "", _as_str),
    # Canonical name first: the two WT_MEDIA_AGENT_LOG_* names are kept working
    # because scripts/start-health.sh:10-11 sets them.
    Field("log_level", "logging.level", ("WT_MEDIA_LOG_LEVEL", "WT_MEDIA_AGENT_LOG_LEVEL"), "INFO", _as_str),
    Field("log_file", "logging.file", ("WT_MEDIA_LOG_FILE", "WT_MEDIA_AGENT_LOG_FILE"), "", _as_str),
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
    data_dir: str
    log_level: str
    log_file: str
    runtime_token: str
    #: Derived from `environment` and `data_dir`, not configured directly.
    paths: RuntimePaths

    @property
    def local_api_address(self) -> str:
        return f"{self.local_api_host}:{self.local_api_port}"

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() == "production"


def default_config_dir() -> Path:
    """The checkout's `config/` directory.

    In a frozen bundle this points inside the extraction directory and simply
    will not exist, which is fine: a missing file means "use the defaults", and
    a bundled sidecar is configured through the environment by Desktop.
    """
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


def _read_document(config_dir: Path | None) -> Mapping[str, object]:
    """Parse `<config_dir>/agent.toml`.

    Missing is not an error -- it means "use the defaults". Malformed is an
    error: a broken file shipping in a release should be loud, not silently
    reinterpreted as an unconfigured agent.
    """
    directory = config_dir if config_dir is not None else default_config_dir()
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
) -> AgentConfig:
    """Resolve the configuration. Reads the filesystem and the environment."""
    environment = os.environ if env is None else env
    document = _read_document(config_dir)
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
