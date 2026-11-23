"""Ephemeral, Profile-scoped HTTP environment. Never use process proxy settings."""
import json
import re
import time
from urllib.parse import quote, urlsplit

class ProfileHttpError(RuntimeError):
    pass

def profile_proxy(config):
    kind = config.get('proxyType')
    if kind in {'noproxy', 'direct'}:
        return None
    schemes = {'http': 'http', 'https': 'https', 'socks5': 'socks5h', 'socks4': 'socks4a'}
    if kind not in schemes:
        raise ProfileHttpError('unsupported Profile proxy')
    host, port = config.get('host'), str(config.get('port', ''))
    if (not isinstance(host, str) or not host or any(c in host for c in '/@?# \r\n')
            or not port.isdecimal() or not 0 < int(port) < 65536):
        raise ProfileHttpError('incomplete Profile proxy')
    if ':' in host and not host.startswith('['):
        host = '[' + host + ']'
    user, password = config.get('proxyUserName') or '', config.get('proxyPassword') or ''
    auth = (quote(str(user), safe='') + ':' + quote(str(password), safe='') + '@') if user or password else ''
    return f'{schemes[kind]}://{auth}{host}:{port}'

class ProfileHttpSession:
    def __init__(self, config, *, allowed_endpoints, cookie_domains, headers=None, session_factory=None):
        self.allowed_endpoints = frozenset(allowed_endpoints)
        self.cookie_domains = frozenset(cookie_domains)
        proxy = profile_proxy(config)
        fingerprint = config.get('browserFingerPrint') or {}
        ua = fingerprint.get('userAgent')
        if not isinstance(ua, str) or not ua or '\r' in ua or '\n' in ua:
            raise ProfileHttpError('missing Profile user agent')
        try:
            cookies = config.get('cookie')
            if isinstance(cookies, str):
                cookies = json.loads(cookies)
            if not isinstance(cookies, list) or not cookies:
                raise ValueError()
        except (ValueError, TypeError):
            raise ProfileHttpError('missing Profile login state') from None
        if session_factory is None:
            from curl_cffi.requests import Session
            session_factory = Session
        self.session = session_factory(trust_env=False)
        self.proxy = proxy
        self.headers = {'Accept': 'application/json, text/plain, */*',
                        **(headers or {}), 'User-Agent': ua}
        chrome = re.search(r'Chrome/(\d+)', ua)
        if chrome:
            major = chrome.group(1)
            self.headers['Sec-CH-UA'] = f'"Chromium";v="{major}", "Google Chrome";v="{major}"'
            self.headers['Sec-CH-UA-Mobile'] = '?1' if 'Mobile' in ua else '?0'
            platform = 'Windows' if 'Windows' in ua else 'macOS' if 'Macintosh' in ua else 'Android' if 'Android' in ua else 'Linux'
            self.headers['Sec-CH-UA-Platform'] = json.dumps(platform)
        language = fingerprint.get('languages')
        if isinstance(language, str) and language and '\r' not in language and '\n' not in language:
            self.headers['Accept-Language'] = language
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            expires = cookie.get('expires', cookie.get('expirationDate'))
            if isinstance(expires, (int, float)) and expires > 0 and expires <= time.time():
                continue
            domain = str(cookie.get('domain', '')).lstrip('.')
            if not any(domain == root or domain.endswith('.' + root) for root in self.cookie_domains):
                continue
            if cookie.get('name') and isinstance(cookie.get('value'), str):
                self.session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'], path=cookie.get('path') or '/')

    def get_json(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or (parsed.netloc, parsed.path) not in self.allowed_endpoints or parsed.fragment or parsed.username:
            raise ProfileHttpError('unexpected platform endpoint')
        try:
            response = self.session.get(url, headers=self.headers, proxy=self.proxy, impersonate='chrome',
                                        default_headers=False, timeout=15, allow_redirects=False)
            if len(response.content) > 4 * 1024 * 1024:
                raise ProfileHttpError('platform response too large')
            try:
                value = response.json()
            except ValueError:
                value = None
            return {'status': response.status_code, 'json': value}
        except ProfileHttpError:
            raise
        except Exception:
            raise ProfileHttpError('platform request unavailable') from None

    def close(self):
        self.session.close()
