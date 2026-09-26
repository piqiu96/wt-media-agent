"""Streaming reads of a short-lived platform CDN address, honouring `Range`.

A download executor may not reach the network itself: ADR-0016 §3 denies
`executors/**` `http`, `urllib` and `socket`, and names clients as where an
executor gets its access. The address is also not an endpoint this repo owns --
it is an arbitrary CDN origin that expires -- so it cannot be registered as an
endpoint-bound client the way the BitBrowser one is. It gets its own opener for
that reason, and this module is the one place that decision is made.

Three facts are reported rather than assumed, each because the caller's next
move depends on it:

- **Whether the server honoured `Range`.** Asked to resume at 100 MiB, a server
  that ignores the header answers `200` with the whole file. A caller that
  believed it had resumed would append the entire file to the part it already
  had and produce a file of the right length and the wrong bytes -- a fault that
  only a digest check catches, hours later and far from its cause. So a `200` to
  a ranged request says so out loud, and the caller discards its part.
- **The total size, when it can be known.** For a ranged response only
  `Content-Range` carries it; `Content-Length` is the length of the part. `-1`
  means unknown, which is not the same as `0` and must not be treated as an
  empty file.
- **Where the body starts.** Echoed so a caller can assert its offset was the
  one served.

Nothing here writes an address into an error. The address *is* a credential: it
is a short-lived signed URL, and `contracts/local-error-codes/v1/transfer.yaml`
names `source_address` in `secret_policy.forbidden_fields` and sets
`raw_upstream_messages_exposed: false`. An error therefore carries a status code
and this module's own words, never the URL and never the upstream body.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Protocol
from urllib import error as urlerror
from urllib import request as urlrequest


class SourceUnavailableError(RuntimeError):
    """The address was refused, has expired, or answered with an error.

    The message never contains the address (a short-lived credential) and never
    quotes the upstream body (`secret_policy.raw_upstream_messages_exposed`).
    """


class SourceStalledError(RuntimeError):
    """The connection stopped producing bytes for longer than the timeout.

    A separate type from `SourceUnavailableError` because the two name different
    operator actions: this one is retried from its offset and, once the attempts
    run out, is written down as `download_stalled`; the other is
    `download_source_unavailable`, which is what a person must re-issue.
    """


class ResponseLike(Protocol):
    """The part of an `http.client.HTTPResponse` this module uses."""

    @property
    def status(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    def read(self, size: int = ...) -> bytes: ...

    def close(self) -> None: ...


#: `(url, headers, timeout) -> response`. Injected so the tests never open a
#: socket; `bootstrap` never builds one of these, because nothing outside an
#: executor drives a download.
Opener = Callable[[str, Mapping[str, str], float], ResponseLike]


def open_source(
    url: str,
    offset: int = 0,
    *,
    opener: Optional[Opener] = None,
    timeout: float = 30.0,
) -> "SourceStream":
    """Open `url` for reading, asking to start at `offset` bytes.

    Raises `SourceUnavailableError` if the address is refused or answers with an
    error, and `SourceStalledError` if the connection times out before it
    answers.
    """
    headers = {"Range": f"bytes={offset}-"} if offset > 0 else {}
    open_url = opener or _urlopen
    try:
        response = open_url(url, headers, timeout)
    except TimeoutError as exc:
        raise SourceStalledError("the source did not answer before the timeout") from exc
    except urlerror.HTTPError as exc:
        # A refused or expired signature is an expected answer here, not a bug:
        # the caller hands Cloud the failure and Cloud re-issues an address.
        code = getattr(exc, "code", 0)
        exc.close()
        raise SourceUnavailableError(f"the source answered with HTTP {code}") from None
    except urlerror.URLError as exc:
        # `URLError.reason` can be a socket error whose text names the host. The
        # address is a credential, so the reason is used only to decide which of
        # the two faults this is, and never rendered.
        reason = getattr(exc, "reason", None)
        if isinstance(reason, TimeoutError):
            raise SourceStalledError("the source did not answer before the timeout") from exc
        raise SourceUnavailableError("the source could not be reached") from exc
    except ValueError:
        # `urlopen` rejects a malformed address itself, before any connection is
        # tried, and its `ValueError` quotes the address it rejected -- the one
        # message in this module that would carry a credential out of it. The
        # exception is deliberately not bound: there is nothing in it this module
        # may repeat. A malformed address is Cloud's to re-issue, so it is an
        # unavailable source and not a crash.
        raise SourceUnavailableError("the source address is not usable") from None
    except OSError as exc:
        raise SourceUnavailableError("the source could not be reached") from exc

    status = int(getattr(response, "status", 0) or 0)
    content_range = _header(response, "content-range")
    start, total = parse_content_range(content_range)

    # `range_honoured` is the whole reason this function reports anything but a
    # body: a `200` to a ranged request means the server sent the entire file,
    # and the caller must throw its part away rather than resume onto it.
    range_honoured = offset == 0 or status == 206
    if offset > 0 and status == 206 and start is not None and start != offset:
        # The server resumed somewhere else. Trusting it would splice two
        # different regions of the file together.
        response.close()
        raise SourceUnavailableError(
            f"the source resumed at byte {start}, not at the byte that was asked for"
        )

    if total is None:
        total = _total_from_length(response, offset, range_honoured)

    return SourceStream(
        response=response,
        url_length=total,
        range_honoured=range_honoured,
        start_offset=offset,
    )


@dataclass
class SourceStream:
    """A readable body plus the two facts about how it was obtained."""

    response: ResponseLike
    url_length: int
    range_honoured: bool
    start_offset: int

    @property
    def total_bytes(self) -> int:
        """The source's full length, or `-1` when the server did not say."""
        return self.url_length

    def read(self, size: int) -> bytes:
        """Read up to `size` bytes; `b""` at the end of the body."""
        try:
            return self.response.read(size)
        except TimeoutError as exc:
            raise SourceStalledError("no bytes arrived before the timeout") from exc
        except OSError as exc:
            # The stream is no longer readable, but the connection did not time
            # out -- a reset part-way through. The caller resumes from the bytes
            # it has, so this is the same operator action as a stall.
            raise SourceStalledError("the connection stopped before the body ended") from exc

    def close(self) -> None:
        self.response.close()

    def __enter__(self) -> "SourceStream":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def parse_content_range(value: Optional[str]) -> tuple[Optional[int], Optional[int]]:
    """`"bytes 100-199/1000"` -> `(100, 1000)`; anything else -> `(None, None)`.

    `"bytes 100-199/*"` gives `(100, None)`: the server answered a ranged request
    without saying how long the whole thing is, which is not the same as the
    whole thing being zero bytes.
    """
    if not value:
        return None, None
    unit, _, spec = value.partition(" ")
    # The unit is checked rather than skipped. `Content-Range` is defined for
    # `bytes` and nothing else, and a header that says something else is a header
    # this module does not understand -- reading its numbers anyway would take a
    # range in an unknown unit for a byte offset and resume at the wrong place.
    if unit.lower() != "bytes":
        return None, None
    span, _, total = spec.partition("/")
    start_text, _, _ = span.partition("-")
    try:
        start = int(start_text)
    except ValueError:
        return None, None
    if total == "*" or not total:
        return start, None
    try:
        return start, int(total)
    except ValueError:
        return start, None


def _header(response: ResponseLike, name: str) -> Optional[str]:
    """A header by name, case-insensitively.

    `http.client` uses an `email.message.Message`, whose lookup is
    case-insensitive; a fake in a test is often a plain dict, whose lookup is
    not. Normalising here keeps a test's spelling from deciding whether the
    production path sees a header.
    """
    headers = response.headers
    getter = getattr(headers, "get", None)
    if getter is not None:
        direct = getter(name)
        if direct is not None:
            return str(direct)
    for key, value in dict(headers).items():
        if str(key).lower() == name.lower():
            return str(value)
    return None


def _total_from_length(response: ResponseLike, offset: int, range_honoured: bool) -> int:
    """Fall back to `Content-Length`, where that length means the whole file.

    The header describes whatever body came with it, so it means the total only
    when the body *is* the whole file -- that is, when no range was asked for, or
    when one was asked for and the server ignored it and sent everything anyway.

    When the range was honoured the body is a part, and `Content-Length` is that
    part's length: adding the offset to it would understate the total by
    however much the server chose to send after the offset, and the executor
    compares the finished file against this number. With no `Content-Range` in
    sight the total is not knowable from this response, which is what `-1` says.
    """
    if offset > 0 and range_honoured:
        return -1
    raw = _header(response, "content-length")
    if raw is None:
        return -1
    try:
        return int(raw)
    except ValueError:
        return -1


def _urlopen(url: str, headers: Mapping[str, str], timeout: float) -> ResponseLike:
    request = urlrequest.Request(url, headers=dict(headers), method="GET")
    return urlrequest.urlopen(request, timeout=timeout)
