"""Aggregate health for the Local Agent and the three dependencies it needs.

`/healthz` is a frozen contract path -- Desktop's `http/local_agent.rs` parses
its three keys -- and nothing here touches it. `/api/v1/health` is the endpoint
architecture baseline §5.6 and ADR-0016 §10 ask for on top of it: Agent version,
Agent status, and the status of Cloud, BitBrowser and Storage.

Two properties are the whole point of this module, and both are about what the
endpoint must *not* do:

* **No outbound write request, for any reason.** No heartbeat, no task claim,
  nothing that changes state on the far side. Cloud's client in this Agent has
  no read-only call at all -- register, heartbeat, claim, report and the two
  sensitive-task permits are every one of them writes -- so Cloud's status
  cannot come from asking Cloud. It comes from the one signal that needs no
  request: whether a socket to the configured host and port can be opened.
  Nothing is sent and no credential is used, which is why what this reports is
  *reachability*, not health: a Cloud that accepts the connection and then
  rejects this Agent's token still reads `normal`. The wider question is
  `/api/v1/status`'s.
* **No exception, ever, because a dependency is unavailable.** Every probe is
  independent, so one dead dependency cannot take the others' answers with it,
  and the endpoint answers 200 either way.

The vocabulary is the one `runtime/environment.py` already reports
(`normal` / `unreachable` / `abnormal`), plus `unknown` -- the ruling's second
degradation value, meaning "this probe could not tell". A probe that fails in a
way nobody anticipated is `unknown` *and* a warning, never a plausible-looking
`abnormal`: folding a `ZeroDivisionError` into a dependency outage is how a real
bug goes unnoticed for a release.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import socket
from typing import Callable, Optional, Protocol
from urllib.parse import urlsplit

from wt_media_agent.clients.bitbrowser import BitBrowserError
from wt_media_agent.local_api.state import LocalAgentState
# The one normalization `/api/v1/status` applies to the same string, imported by
# its underscore name as `local_api/reporting.py`'s helpers already are, so the
# two endpoints cannot report two different Agent versions.
from wt_media_agent.runtime.environment import _version_only
from wt_media_agent.runtime.version import __version__

logger = logging.getLogger(__name__)

SERVICE = "wt-media-agent"

#: The roll-up value when every dependency answered `normal`.
OK = "ok"
#: A dependency answered, and answered for itself.
NORMAL = "normal"
#: BitBrowser's local API could not be reached. Named exactly as
#: `RuntimeEnvironmentReport.bitbrowser_status` names it, so an operator can
#: compare the two endpoints' answers line by line.
UNREACHABLE = "unreachable"
#: A dependency this endpoint needs is not answering, and it is not a
#: reachability question with a partial answer.
ABNORMAL = "abnormal"
#: The probe could not tell -- no endpoint configured, or a failure nobody
#: anticipated. Not `abnormal`: that would claim a knowledge nobody has.
UNKNOWN = "unknown"

#: Short on purpose: this probe runs inside an HTTP request and in front of a
#: caller (Desktop's health poll) that would rather have an answer than a
#: complete one.
DEFAULT_CLOUD_PROBE_TIMEOUT = 2.0


class GroupLister(Protocol):
    def group_list(self) -> list[dict[str, object]]: ...


class StorageProbe(Protocol):
    def probe(self) -> None: ...


class ClosableConnection(Protocol):
    def close(self) -> None: ...


#: The only outbound call any probe here is allowed to make. Injected so the
#: branch table is testable without a network, and typed as closed rather than
#: used, so a probe cannot quietly start sending something.
TcpConnect = Callable[[tuple[str, int], float], ClosableConnection]


@dataclass(frozen=True)
class CloudReachability:
    """Cloud's status, from a socket and nothing else.

    `status()` raises `OSError` when the connection cannot be opened (a refusal
    and a timeout both land there); `health_report` is what turns that into
    `abnormal`. An empty or non-HTTP `base_url` is `unknown`: there is no
    endpoint to probe, which is not the same answer as an endpoint that is down.
    """

    base_url: str = ""
    timeout: float = DEFAULT_CLOUD_PROBE_TIMEOUT
    connect: TcpConnect = socket.create_connection

    def status(self) -> str:
        target = _host_and_port(self.base_url)
        if target is None:
            return UNKNOWN
        connection = self.connect(target, self.timeout)
        # Closed immediately: the question was whether a connection could be
        # established, not what could be done with one.
        connection.close()
        return NORMAL


@dataclass(frozen=True)
class HealthReport:
    agent_version: str
    agent_status: str
    cloud: str
    bitbrowser: str
    storage: str

    @property
    def status(self) -> str:
        """`ok` only when all three dependencies answered `normal`.

        `unknown` counts as degraded: "nobody can tell" is not "fine", and a
        roll-up that called it `ok` would be the one field a monitor must not
        trust.
        """
        if all(value == NORMAL for value in (self.cloud, self.bitbrowser, self.storage)):
            return OK
        return ABNORMAL

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "service": SERVICE,
            "agent_version": self.agent_version,
            "agent_status": self.agent_status,
            "dependencies": {
                "cloud": {"status": self.cloud},
                "bitbrowser": {"status": self.bitbrowser},
                "storage": {"status": self.storage},
            },
        }


def health_report(
    state: LocalAgentState,
    bitbrowser: GroupLister,
    store: Optional[StorageProbe],
    cloud: CloudReachability,
) -> HealthReport:
    """Aggregate the Agent's own facts and the three dependency probes.

    Deliberately not `/api/v1/status`: that one runs a full profile scan, reads
    the database and can take seconds, and this endpoint is cheap enough to be
    polled.
    """
    return HealthReport(
        agent_version=_version_only(__version__),
        agent_status=state.status,
        cloud=_cloud_status(cloud),
        bitbrowser=_bitbrowser_status(bitbrowser),
        storage=_storage_status(store),
    )


def _cloud_status(probe: CloudReachability) -> str:
    try:
        return probe.status()
    except OSError as error:
        # Refused, timed out, DNS failure: Cloud is configured and not there.
        _degraded("cloud", error)
        return ABNORMAL
    except Exception as error:
        _unexpected("cloud", error)
        return UNKNOWN


def _bitbrowser_status(client: GroupLister) -> str:
    """Reachability only: one group query, which is a read.

    `/api/v1/status` runs the full `scan_profiles` and can therefore also report
    `identity_unverifiable`. This endpoint deliberately does not -- a health
    check that verified profile ownership would open a profile scan on every
    poll -- so `unreachable` is the whole of its bad news.
    """
    try:
        client.group_list()
    except BitBrowserError as error:
        _degraded("bitbrowser", error)
        return UNREACHABLE
    except Exception as error:
        _unexpected("bitbrowser", error)
        return UNKNOWN
    return NORMAL


def _storage_status(store: Optional[StorageProbe]) -> str:
    if store is None:
        return UNKNOWN
    try:
        store.probe()
    except Exception as error:
        # Broad on purpose: whatever it was, this Agent cannot read its own task
        # table, and that is never routine -- so this one keeps its traceback.
        _unexpected("storage", error)
        return ABNORMAL
    return NORMAL


def _degraded(dependency: str, error: BaseException) -> None:
    """A dependency that is down, as one line and no traceback.

    A refused Cloud and an absent BitBrowser are the routine outages this
    endpoint exists to report, and a viewer that polls it would otherwise repeat
    a full traceback into `agent.log` for as long as the outage lasts. The
    message carries the reason; only `_unexpected` needs the stack.
    """
    logger.warning("local_api.health.%s.degraded error=%s", dependency, error)


def _unexpected(dependency: str, error: BaseException) -> None:
    """A probe that failed in a way nobody anticipated, with the traceback.

    "The endpoint said unhealthy" and "the probe raised" are different facts,
    and only the second one can be diagnosed.
    """
    logger.warning(
        "local_api.health.%s.degraded error=%s", dependency, error, exc_info=True
    )


def _host_and_port(base_url: str) -> Optional[tuple[str, int]]:
    """The Cloud host and port to connect to, or None if there is not one."""
    try:
        parts = urlsplit(base_url.strip())
        port = parts.port
    except ValueError:
        # A port that is not a number. Configuration error, not an outage.
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return (parts.hostname, port or (443 if parts.scheme == "https" else 80))
