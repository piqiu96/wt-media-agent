"""The construction recipe for `BitBrowserClient` (CHG-056 T-03).

`clients/bitbrowser/factory.py` exists so that exactly one place decides which
configuration key supplies which constructor parameter. Every test here drives
the real `load_config` and then observes the *request* the resulting client
makes, so a broken recipe fails on the wire rather than on an attribute name.

The two properties worth protecting:

1. the configured values actually arrive (url, base timeout, both overrides);
2. nothing reads the environment after construction -- an override can only
   raise a timeout, never appear later from a variable set in the meantime.
"""

from __future__ import annotations

from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

from wt_media_agent.clients.bitbrowser import bitbrowser_from_config
from wt_media_agent.clients.bitbrowser.timeouts import DEFAULT_OPERATION_TIMEOUT
from wt_media_agent.runtime.config import load_config

#: The environment this recipe genuinely reads. Cleared for each test so the
#: developer's own shell cannot decide the outcome.
CONTROLLED_KEYS = (
    "WT_MEDIA_BITBROWSER_API_URL",
    "WT_MEDIA_BITBROWSER_TIMEOUT_SECONDS",
    "WT_MEDIA_BITBROWSER_CREATE_TIMEOUT_SECONDS",
    "WT_MEDIA_BITBROWSER_MUTATION_TIMEOUT_SECONDS",
)


class FakeTransport:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object], float]] = []

    def __call__(self, url: str, payload: dict[str, object], timeout: float) -> dict[str, object]:
        self.calls.append((url, payload, timeout))
        return self.responses.pop(0)


def profile(index: int = 1) -> dict[str, object]:
    return {
        "id": f"profile-{index}",
        "name": f"窗口 {index}",
        "seq": index,
        "groupId": "group-1",
        "groupName": "运营组",
        "status": 1,
        "userId": "bit-user-1",
        "mainUserId": "main-user-1",
        "updateTime": "2026-07-14 18:00:00",
    }


class FactoryTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = {key: os.environ.pop(key, None) for key in CONTROLLED_KEYS}
        self._tmp = tempfile.TemporaryDirectory()
        # An empty config directory: every value in these tests comes from the
        # environment or from the built-in default, never from the shipped file.
        self.config_dir = Path(self._tmp.name)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def build(self, transport: FakeTransport, env: dict[str, str] | None = None):
        config = load_config(config_dir=self.config_dir, env=env or {})
        return bitbrowser_from_config(config, transport=transport), config


class ConfigurationReachesTheClientTest(FactoryTestCase):
    def test_the_configured_base_url_is_used_and_normalised(self):
        transport = FakeTransport([{"success": True, "data": {"list": [profile()]}}])
        client, _ = self.build(
            transport, {"WT_MEDIA_BITBROWSER_API_URL": "http://10.0.0.9:1234/"}
        )

        client.scan_profiles()

        self.assertEqual(transport.calls[0][0], "http://10.0.0.9:1234/browser/list")

    def test_the_configured_timeout_reaches_the_transport(self):
        transport = FakeTransport([{"success": True, "data": {"list": [profile()]}}])
        client, _ = self.build(transport, {"WT_MEDIA_BITBROWSER_TIMEOUT_SECONDS": "7.5"})

        client.scan_profiles()

        self.assertEqual(transport.calls[0][2], 7.5)

    def test_the_create_override_reaches_the_transport(self):
        transport = FakeTransport([{"success": True, "data": {"id": "created"}}])
        client, _ = self.build(
            transport, {"WT_MEDIA_BITBROWSER_CREATE_TIMEOUT_SECONDS": "45"}
        )

        client.create_profile({})

        self.assertEqual(transport.calls[0][2], 45.0)

    def test_the_mutation_override_reaches_the_transport(self):
        transport = FakeTransport([{"success": True, "data": {}}])
        client, _ = self.build(
            transport, {"WT_MEDIA_BITBROWSER_MUTATION_TIMEOUT_SECONDS": "60"}
        )

        client.open_profile("profile-1")

        self.assertEqual(transport.calls[0][2], 60.0)


class TimeoutFloorTest(FactoryTestCase):
    """An override is a floor-raiser, never a way to shorten an operation."""

    def test_an_unset_override_falls_back_to_the_operation_default(self):
        transport = FakeTransport([{"success": True, "data": {"id": "created"}}])
        client, _ = self.build(transport, {"WT_MEDIA_BITBROWSER_TIMEOUT_SECONDS": "2"})

        client.create_profile({})

        # Not 2.0: an unconfigured override means "use the operation's own
        # default", which is deliberately longer than the scan timeout.
        self.assertEqual(transport.calls[0][2], DEFAULT_OPERATION_TIMEOUT)

    def test_an_override_below_the_configured_timeout_loses(self):
        transport = FakeTransport([{"success": True, "data": {"id": "created"}}])
        client, _ = self.build(
            transport,
            {
                "WT_MEDIA_BITBROWSER_TIMEOUT_SECONDS": "100",
                "WT_MEDIA_BITBROWSER_CREATE_TIMEOUT_SECONDS": "45",
            },
        )

        client.create_profile({})

        self.assertEqual(transport.calls[0][2], 100.0)


class NoLateEnvironmentReadsTest(FactoryTestCase):
    """The client is configured once, at construction.

    Before T-03 the client read these two variables itself, on every call. A
    variable exported after the client was built would therefore silently change
    its behaviour. The client now has no `os` import at all; this test is what
    makes that recoverable if one comes back.
    """

    def test_a_variable_set_after_construction_is_not_observed(self):
        transport = FakeTransport([{"success": True, "data": {"id": "created"}}])
        client, _ = self.build(transport, {"WT_MEDIA_BITBROWSER_TIMEOUT_SECONDS": "2"})

        with mock.patch.dict(
            os.environ, {"WT_MEDIA_BITBROWSER_CREATE_TIMEOUT_SECONDS": "45"}
        ):
            client.create_profile({})

        self.assertEqual(transport.calls[0][2], DEFAULT_OPERATION_TIMEOUT)

    def test_the_module_does_not_import_os(self):
        from wt_media_agent.clients.bitbrowser import client as client_module

        self.assertFalse(hasattr(client_module, "os"))


if __name__ == "__main__":
    unittest.main()
