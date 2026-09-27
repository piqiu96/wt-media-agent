"""The Cloud node credential this process is bound with, held in memory only.

The four Cloud-Agent transfer calls are identified by this string and by nothing
else: `contracts/cloud-agent-api/v1/file-transfer.openapi.yaml` gives `claim`
neither a path parameter nor a request body, so there is no other way for Cloud
to know which node is asking. It is not a configuration value -- Cloud issues it
once from `POST /api/v1/local-agent/nodes/register`, Desktop hands it to this
process on the bind call, and it is valid until Cloud replaces it. So it lives
here, in memory, and it is gone when the process is (the user's ruling of
2026-09-27: Desktop forwards it, the Agent does not write it down).

**Why `runtime/`.** Two layers need the same object and neither may import the
other: `local_api/` writes it (the bind route) and `runner/` reads it (the
transfer loop). Both already descend into `runtime/`, so this adds no layer edge
to the ratchet -- while `local_api/` would make the runner import the API
surface it sits beside, and `storage/` would invite somebody to persist a
credential whose whole contract here is that it is never written down.

**What it does not do.** It never says what it holds: `__repr__` answers whether
a credential is set and not which one, so an f-string, a `%s` or a debugging
`extra={...}` cannot make one reach a log line. The reader is `get`, and it is
handed to the transfer loop as a callable rather than read once at assembly,
because Cloud can replace the credential while a download is running.
"""

from __future__ import annotations

from typing import Optional


class NodeCredential:
    """The node credential this process is currently bound with."""

    def __init__(self) -> None:
        self._value: Optional[str] = None

    def get(self) -> Optional[str]:
        """The credential, or `None` when this process has not been bound.

        `None` is not a failure. A freshly started Agent has no credential until
        Desktop binds it, and the transfer loop answers "nothing to claim" to an
        unbound process instead of reporting a fault that is not one.
        """
        return self._value

    def set(self, credential: str) -> None:
        """Record the credential Cloud issued for this node.

        There is deliberately no `clear`. The bind call is the only writer, and
        the caller that has a credential to send is the one that just obtained
        it from Cloud; a bind carrying none is the local-only bind the Desktop
        has always been able to make, and it must not be able to unbind a
        running download loop.
        """
        self._value = credential

    def __repr__(self) -> str:
        return f"<NodeCredential {'set' if self._value else 'unset'}>"
