"""Cloud-Agent API client and consumer-side contract checks.

The only module in the Agent that talks to the Cloud Cloud-Agent API. Per
ADR-0016 nothing outside `clients/` may construct `CloudAgentClient`; it is
built once in bootstrap and injected inward.
"""

from wt_media_agent.clients.cloud.client import (
    AgentIdentity,
    CloudAgentClient,
    SessionInvalidError,
    Transport,
)
from wt_media_agent.clients.cloud.contract import (
    API_NAME,
    EXPECTED_MAJOR_VERSION,
    REQUIRED_CONTRACT_REVISION,
    is_cloud_agent_compatible,
    is_cloud_response_compatible,
)
from wt_media_agent.clients.cloud.transfer import (
    TransferError,
    TransferIntegrityRejectedError,
    TransferLease,
    TransferLeaseLostError,
    TransferTerminal,
    TransferTransport,
    TransferUnavailableError,
)

__all__ = [
    "API_NAME",
    "AgentIdentity",
    "CloudAgentClient",
    "EXPECTED_MAJOR_VERSION",
    "REQUIRED_CONTRACT_REVISION",
    "SessionInvalidError",
    "TransferError",
    "TransferIntegrityRejectedError",
    "TransferLease",
    "TransferLeaseLostError",
    "TransferTerminal",
    "TransferTransport",
    "TransferUnavailableError",
    "Transport",
    "is_cloud_agent_compatible",
    "is_cloud_response_compatible",
]
