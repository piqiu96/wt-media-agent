"""Deprecated import path. Use `wt_media_agent.clients.cloud`.

Retained as a re-export shim because `tests/test_runner_session.py` imports
`SessionInvalidError` from here and that test must stay byte-identical through
the T-02 migration. Retire this shim in CHG-B/C.
"""

from wt_media_agent.clients.cloud.client import (
    AgentIdentity,
    CloudAgentClient,
    SessionInvalidError,
    Transport,
)

__all__ = ["AgentIdentity", "CloudAgentClient", "SessionInvalidError", "Transport"]
