"""Cloud-Agent API 客户端与契约兼容检查。

Agent 里唯一访问 Cloud Agent API 的模块。按 ADR-0016，只有 `clients/` 可以
构造 `CloudAgentClient`，bootstrap 构造一次后向内注入。
"""

from wt_media_agent.clients.cloud.client import CloudAgentClient, Transport
from wt_media_agent.clients.cloud.contract import (
    API_NAME,
    EXPECTED_MAJOR_VERSION,
    REQUIRED_CONTRACT_REVISION,
    is_cloud_agent_compatible,
    is_cloud_response_compatible,
)
from wt_media_agent.clients.cloud.errors import (
    AgentAuthError,
    CloudError,
    CloudUnavailableError,
    NoPendingTaskError,
    StaleAttemptError,
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
    "AgentAuthError",
    "CloudAgentClient",
    "CloudError",
    "CloudUnavailableError",
    "EXPECTED_MAJOR_VERSION",
    "NoPendingTaskError",
    "REQUIRED_CONTRACT_REVISION",
    "StaleAttemptError",
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
