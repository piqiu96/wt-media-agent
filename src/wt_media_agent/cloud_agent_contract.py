"""Deprecated import path. Use `wt_media_agent.clients.cloud.contract`.

Retained as a re-export shim for symmetry with `cloud_agent_client.py`.
Retire this shim in CHG-B/C.
"""

from wt_media_agent.clients.cloud.contract import (
    API_NAME,
    EXPECTED_MAJOR_VERSION,
    REQUIRED_CONTRACT_REVISION,
    is_cloud_agent_compatible,
    is_cloud_response_compatible,
)

__all__ = [
    "API_NAME",
    "EXPECTED_MAJOR_VERSION",
    "REQUIRED_CONTRACT_REVISION",
    "is_cloud_agent_compatible",
    "is_cloud_response_compatible",
]
