"""Read the live Baidu UID through the owned Profile's network stack."""
from wt_media_agent.clients.baidu.identity import is_logininfo_url, read_identity
from wt_media_agent.services.browser.session import BrowserSession


def _identity_request_only(route):
    request = route.request
    if request.method == 'GET' and is_logininfo_url(request.url):
        route.fallback()
    else:
        route.abort()


def _read_identity_tab(page):
    try:
        page.route('**/*', _identity_request_only)
        def fetch(url):
            response = page.goto(url, wait_until='domcontentloaded', timeout=15000)
            return {'status': response.status if response else 0,
                    'json': response.json() if response and response.status == 200 else None}
        identity = read_identity(fetch)
        return {key: identity[key] for key in ('login_status', 'platform_account_id')}
    finally:
        page.close()


def read_baidu_identity(endpoint, *, browser_context=None, browser_session_factory=BrowserSession):
    """No Cookie export or local HTTP fallback; use the existing Profile only."""
    if browser_context is not None:
        # Re-entering sync_playwright while the editor is active is invalid.
        # A tab in its context uses the same Profile and closes independently.
        return _read_identity_tab(browser_context.new_page())
    with browser_session_factory(endpoint) as session:
        return _read_identity_tab(session.page)
