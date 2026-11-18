"""One local BitBrowser connection; disconnect never proves scene closure."""
from urllib.parse import urlsplit


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
