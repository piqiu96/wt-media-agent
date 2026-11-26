"""Read the live Bilibili UID through the owned Profile; never infer from cookies."""
from wt_media_agent.clients.bilibili.identity import NAV_URL, identify_bilibili
from wt_media_agent.services.browser.session import BrowserSession


def _identity_only(route):
    if route.request.method == 'GET' and route.request.url == NAV_URL:
        route.fallback()
    else:
        route.abort()


def _read_tab(page):
    try:
        page.route('**/*', _identity_only)
        response = page.goto(NAV_URL, wait_until='domcontentloaded', timeout=15000)
        result = identify_bilibili([], response.json() if response and response.status == 200 else None)
        return {key: result[key] for key in ('login_status', 'platform_account_id')}
    finally:
        page.close()


def read_bilibili_identity(endpoint, *, browser_context=None, browser_session_factory=BrowserSession):
    if browser_context is not None:
        return _read_tab(browser_context.new_page())
    with browser_session_factory(endpoint) as session:
        return _read_tab(session.page)
