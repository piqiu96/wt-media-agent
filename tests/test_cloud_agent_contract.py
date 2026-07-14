from __future__ import annotations

import unittest

from wt_media_agent.cloud_agent_contract import (
    REQUIRED_CONTRACT_REVISION,
    is_cloud_agent_compatible,
    is_cloud_response_compatible,
)


def current_metadata() -> dict[str, object]:
    return {
        "api": "cloud-agent",
        "major_version": "v1",
        "contract_revision": REQUIRED_CONTRACT_REVISION,
        "minimum_agent_contract_revision": REQUIRED_CONTRACT_REVISION,
        "compatible_agent_major_versions": ["v1"],
        "status": "compatible",
    }


class CloudAgentContractCompatibilityTest(unittest.TestCase):
    def test_current_cloud_metadata_is_compatible(self) -> None:
        self.assertTrue(is_cloud_agent_compatible(current_metadata()))

    def test_cloud_response_envelope_is_compatible(self) -> None:
        self.assertTrue(is_cloud_response_compatible({"data": current_metadata()}))

    def test_newer_revision_is_compatible(self) -> None:
        metadata = current_metadata()
        metadata["contract_revision"] = "2026.07.14.7"

        self.assertTrue(is_cloud_agent_compatible(metadata))

    def test_wrong_major_is_incompatible(self) -> None:
        metadata = current_metadata()
        metadata["major_version"] = "v2"

        self.assertFalse(is_cloud_agent_compatible(metadata))

    def test_older_revision_is_incompatible(self) -> None:
        metadata = current_metadata()
        metadata["contract_revision"] = "2026.07.13.1"

        self.assertFalse(is_cloud_agent_compatible(metadata))

    def test_missing_agent_major_is_incompatible(self) -> None:
        metadata = current_metadata()
        metadata["compatible_agent_major_versions"] = ["v2"]

        self.assertFalse(is_cloud_agent_compatible(metadata))

    def test_malformed_response_is_incompatible(self) -> None:
        self.assertFalse(is_cloud_response_compatible({"data": "bad"}))


if __name__ == "__main__":
    unittest.main()
