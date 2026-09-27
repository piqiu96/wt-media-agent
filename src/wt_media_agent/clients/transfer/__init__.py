"""File-transfer clients: the byte source a download reads from.

`source.py` reads a short-lived platform CDN address. It is a client and not a
service because the address is an external system's, reached over HTTP, exactly
like `clients/cloud/`; ADR-0016 §2 puts "调外部系统" here.

The *sink* — where those bytes land on this machine — is deliberately not in
this package. It is `storage/download_sink.py`, because writing files is not an
external protocol.
"""

from wt_media_agent.clients.transfer.source import (
    FALLBACK_EXTENSION,
    Opener,
    ResponseLike,
    SourceStalledError,
    SourceStream,
    SourceUnavailableError,
    extension_from_url,
    open_source,
)

__all__ = [
    "FALLBACK_EXTENSION",
    "Opener",
    "ResponseLike",
    "SourceStalledError",
    "SourceStream",
    "SourceUnavailableError",
    "extension_from_url",
    "open_source",
]
