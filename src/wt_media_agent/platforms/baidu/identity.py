"""Read the live Baidu UID through the owned Profile's network stack."""
import time
from urllib.parse import urlsplit

from wt_media_agent.clients.baidu.identity import LOGININFO_URL_TEMPLATE
from wt_media_agent.services.browser.session import BrowserSession


def _identity_request_only(route):
    request = route.request
    parsed = urlsplit(request.url)
    if (request.method == 'GET' and parsed.scheme == 'https'
            and parsed.netloc == 'image.baidu.com' and parsed.path == '/user/logininfo'):
        route.fallback()
    else:
        route.abort()


def _read_identity_tab(page):
    try:
        page.route('**/*', _identity_request_only)
        response = page.goto(LOGININFO_URL_TEMPLATE.format(time_ms=int(time.time() * 1000)),
                             wait_until='domcontentloaded', timeout=15000)
        body = response.json() if response and response.status == 200 else {}
        value = ((body.get('data') or {}).get('user') or {}).get('user_id')
        uid = str(value) if type(value) in (str, int) else ''
        valid = bool(uid) and uid.isascii() and uid.isdecimal() and uid[0] != '0'
        return {'login_status': 'normal' if valid else 'not_logged_in',
                'platform_account_id': uid if valid else ''}
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
