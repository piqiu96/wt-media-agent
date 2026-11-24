"""One local BitBrowser connection; disconnect never proves scene closure."""
from urllib.parse import urlsplit
from contextlib import contextmanager


class BrowserSession:
    def __init__(self, endpoint, *, playwright_factory=None):
        parsed = urlsplit('http://' + endpoint)
        if (parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or not parsed.port
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError('browser endpoint must be a local host and port')
        self.endpoint = 'http://' + endpoint
        self._factory = playwright_factory
        self._runtime = None
        self.page = None

    def __enter__(self):
        if self._factory is None:
            from playwright.sync_api import sync_playwright
            self._factory = sync_playwright
        manager = self._factory()
        try:
            playwright = manager.start()
            self._runtime = playwright
            browser = playwright.chromium.connect_over_cdp(
                self.endpoint, is_local=True, timeout=30000)
            if len(browser.contexts) != 1:
                raise RuntimeError('expected the existing BitBrowser default context')
            self.page = browser.contexts[0].new_page()
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        runtime, self._runtime = self._runtime, None
        if runtime is not None:
            runtime.stop()  # disconnect; retain editor for reconciliation

    def __exit__(self, *_):
        self.close()


class ProfileReadSession:
    """Read resources for one closed Profile, under the caller's operation lock."""
    def __init__(self, bitbrowser, profile_id, *, before_open=lambda: None, opened=lambda endpoint: None):
        self.bitbrowser = bitbrowser
        self.profile_id = profile_id
        self.closed = False
        self.browser_used = False
        self._opening_attempted = False
        self._before_open = before_open
        self._opened = opened

    def __enter__(self):
        self.closed = self.bitbrowser.is_profile_closed(self.profile_id) is True
        if not self.closed:
            raise RuntimeError('original Profile is occupied')
        return self

    def read_environment(self):
        return self.bitbrowser.read_request_environment(self.profile_id)

    @contextmanager
    def browser(self):
        if self._opening_attempted:
            raise RuntimeError('read browser cannot be reopened')
        self._before_open()
        self._opening_attempted = True
        self.closed = False  # Invalidate even if the open reply is lost.
        endpoint = self.bitbrowser.open_profile_for_check(self.profile_id)
        self.browser_used = True
        try:
            self._opened(endpoint)
            yield endpoint
        finally:
            self.closed = self.bitbrowser.close_profile_verified(self.profile_id, endpoint) is True

    def __exit__(self, exc_type, *_):
        if exc_type is None and not self._opening_attempted:
            self.closed = False
            self.closed = self.bitbrowser.is_profile_closed(self.profile_id) is True
