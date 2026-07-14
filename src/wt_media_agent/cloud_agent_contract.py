"""Consumer-side Cloud-Agent contract compatibility checks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Optional


API_NAME = "cloud-agent"
EXPECTED_MAJOR_VERSION = "v1"
REQUIRED_CONTRACT_REVISION = "2026.07.14.7"


def is_cloud_agent_compatible(metadata: Mapping[str, object]) -> bool:
    """Return whether Cloud's compatibility metadata can be consumed by this Agent."""

    if metadata.get("api") != API_NAME:
        return False
    if metadata.get("major_version") != EXPECTED_MAJOR_VERSION:
        return False
    if metadata.get("status") != "compatible":
        return False
    if EXPECTED_MAJOR_VERSION not in _string_list(metadata.get("compatible_agent_major_versions")):
        return False

    revision = metadata.get("contract_revision")
    if not isinstance(revision, str):
        return False
    return _compare_revision(revision, REQUIRED_CONTRACT_REVISION) >= 0


def is_cloud_response_compatible(response: Mapping[str, object]) -> bool:
    """Accept the standard Cloud `{data: ...}` envelope used by the compatibility endpoint."""

    data = response.get("data")
    if not isinstance(data, Mapping):
        return False
    return is_cloud_agent_compatible(data)


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _compare_revision(left: str, right: str) -> int:
    left_parts = _parse_revision(left)
    right_parts = _parse_revision(right)
    if left_parts is None or right_parts is None:
        return -1
    if left_parts > right_parts:
        return 1
    if left_parts < right_parts:
        return -1
    return 0


def _parse_revision(value: str) -> Optional[tuple[int, int, int, int]]:
    parts = value.split(".")
    if len(parts) != 4:
        return None
    try:
        parsed = tuple(int(part) for part in parts)
    except ValueError:
        return None
    if len(parsed) != 4:
        return None
    return parsed
