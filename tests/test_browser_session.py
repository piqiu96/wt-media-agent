"""Local CDP ownership and cleanup; no platform request is made here."""
import importlib.util
import unittest
from unittest.mock import MagicMock


class BrowserSessionTests(unittest.TestCase):
    def session_class(self):
        name = 'wt_media_agent.services.browser.session'
        self.assertIsNotNone(importlib.util.find_spec(name), 'Playwright local session is missing')
        from wt_media_agent.services.browser.session import BrowserSession
        return BrowserSession

    def test_attaches_to_same_host_with_path_optimization_and_disconnects_only(self):
        Session = self.session_class()
        runtime = MagicMock()
        browser = runtime.start.return_value.chromium.connect_over_cdp.return_value
        context = MagicMock()
        browser.contexts = [context]
        page = context.new_page.return_value
        with Session('127.0.0.1:39871', playwright_factory=lambda: runtime) as session:
            self.assertIs(session.page, page)
        runtime.start.return_value.chromium.connect_over_cdp.assert_called_once_with(
            'http://127.0.0.1:39871', is_local=True, timeout=30000)
        runtime.start.return_value.stop.assert_called_once()
        browser.close.assert_not_called()
        page.close.assert_not_called()  # retained for reconciliation, not destroyed

    def test_remote_or_credentialed_endpoint_never_starts_browser_driver(self):
        Session = self.session_class()
        for endpoint in ('remote.test:9222', 'user:pass@127.0.0.1:9222', '127.0.0.1:9222/path', '127.0.0.1:9222?query=1'):
            runtime = MagicMock()
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                with Session(endpoint, playwright_factory=lambda: runtime):
                    self.fail('invalid endpoint attached')
            runtime.start.assert_not_called()

    def test_connect_failure_stops_driver_without_masking_original_failure(self):
        Session = self.session_class()
        runtime = MagicMock()
        runtime.start.return_value.chromium.connect_over_cdp.side_effect = ConnectionError('unavailable')
        with self.assertRaises(ConnectionError):
            with Session('127.0.0.1:39871', playwright_factory=lambda: runtime):
                self.fail('attached')
        runtime.start.return_value.stop.assert_called_once()
