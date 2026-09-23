import unittest

from wt_media_agent.clients.bitbrowser import BitBrowserClient
from wt_media_agent.clients.bitbrowser.timeouts import (
    DEFAULT_OPERATION_TIMEOUT,
    effective_timeout,
)
from wt_media_agent.runtime.config import load_config


class FakeTransport:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object], float]] = []

    def __call__(self, url: str, payload: dict[str, object], timeout: float) -> dict[str, object]:
        self.calls.append((url, payload, timeout))
        return self.responses.pop(0)


class EffectiveTimeoutTest(unittest.TestCase):
    """The rule the pre-T-03 `max(default_timeout, timeout)` implemented."""

    def test_no_override_falls_back_to_the_operation_default(self):
        self.assertEqual(effective_timeout(5.0, None, 30.0), 30.0)

    def test_a_raising_override_wins(self):
        self.assertEqual(effective_timeout(5.0, 45.0, 30.0), 45.0)

    def test_an_override_cannot_lower_the_configured_timeout(self):
        """The load-bearing direction: a mistake may stall, never truncate."""
        self.assertEqual(effective_timeout(5.0, 2.0, 30.0), 5.0)

    def test_a_configured_timeout_above_the_default_wins(self):
        self.assertEqual(effective_timeout(60.0, None, 30.0), 60.0)


class ClientSeamTest(unittest.TestCase):
    def test_the_operation_default_is_the_single_source_for_30(self):
        self.assertEqual(BitBrowserClient.DEFAULT_CREATE_TIMEOUT, DEFAULT_OPERATION_TIMEOUT)
        self.assertEqual(BitBrowserClient.DEFAULT_MUTATION_TIMEOUT, DEFAULT_OPERATION_TIMEOUT)

    def test_overrides_reach_the_transport(self):
        transport = FakeTransport([
            {"success": True, "data": {"id": "p"}},
            {"success": True, "data": {}},
        ])
        client = BitBrowserClient(
            "http://127.0.0.1:54345",
            transport=transport,
            timeout=2,
            create_timeout_override=45.0,
            mutation_timeout_override=60.0,
        )
        client.create_profile({})
        client.open_profile("profile-1")
        self.assertEqual([call[2] for call in transport.calls], [45.0, 60.0])

    def test_the_base_timeout_still_governs_the_non_operation_calls(self):
        """`/browser/detail` is not a create or a mutation: no override applies."""
        transport = FakeTransport([
            {"success": True, "data": {"browserFingerPrint": {"ua": "x"}, "proxyMethod": 2}},
            {"success": True, "data": {}},
        ])
        client = BitBrowserClient(
            "http://127.0.0.1:54345",
            transport=transport,
            timeout=2,
            mutation_timeout_override=60.0,
        )
        client.update_profile("profile-1", {"name": "窗口"})
        self.assertEqual([call[2] for call in transport.calls], [2.0, 60.0])


class ConfigurationPathTest(unittest.TestCase):
    """Both override variables, from the environment all the way to the wire."""

    def config(self, **env: str):
        return load_config(env=env)

    def test_the_create_override_travels_from_the_environment(self):
        cfg = self.config(WT_MEDIA_BITBROWSER_CREATE_TIMEOUT_SECONDS="45")
        transport = FakeTransport([{"success": True, "data": {"id": "p"}}])
        BitBrowserClient(
            "http://127.0.0.1:54345",
            transport=transport,
            timeout=2,
            create_timeout_override=cfg.bitbrowser_create_timeout_seconds,
        ).create_profile({})
        self.assertEqual(transport.calls[0][2], 45.0)

    def test_the_mutation_override_travels_from_the_environment(self):
        cfg = self.config(WT_MEDIA_BITBROWSER_MUTATION_TIMEOUT_SECONDS="60")
        transport = FakeTransport([{"success": True, "data": {}}])
        BitBrowserClient(
            "http://127.0.0.1:54345",
            transport=transport,
            timeout=2,
            mutation_timeout_override=cfg.bitbrowser_mutation_timeout_seconds,
        ).open_profile("profile-1")
        self.assertEqual(transport.calls[0][2], 60.0)

    def test_unset_overrides_leave_the_operation_default_in_place(self):
        cfg = self.config()
        self.assertIsNone(cfg.bitbrowser_create_timeout_seconds)
        transport = FakeTransport([{"success": True, "data": {"id": "p"}}])
        BitBrowserClient(
            "http://127.0.0.1:54345",
            transport=transport,
            timeout=2,
            create_timeout_override=cfg.bitbrowser_create_timeout_seconds,
        ).create_profile({})
        self.assertEqual(transport.calls[0][2], DEFAULT_OPERATION_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
