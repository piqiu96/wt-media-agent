"""Six public counters for an exact aid/owner; no cookies or browser session."""
import json
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

VIEW_URL = 'https://api.bilibili.com/x/web-interface/view'
FIELDS = ('view', 'like', 'favorite', 'coin', 'reply', 'danmaku')


class MetricsError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def decimal_id(value):
    return (isinstance(value, str) and value.isascii() and value.isdecimal()
            and 0 < len(value) <= 20 and value[0] != '0')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_public(aid):
    # Fixed origin and path; neither Cloud nor a work link supplies a fetch URL.
    req = Request(VIEW_URL + '?aid=' + aid, headers={
        'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/',
        'Accept': 'application/json',
    })
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(req, timeout=15) as response:
            if response.status != 200:
                raise MetricsError('unavailable')
            body = response.read(2 * 1024 * 1024 + 1)
            if len(body) > 2 * 1024 * 1024:
                raise MetricsError('invalid_response')
            return json.loads(body)
    except MetricsError:
        raise
    except HTTPError as error:
        error.close()
        raise MetricsError('rate_limited' if error.code in (412, 429) else 'unavailable') from None
    except (URLError, TimeoutError, OSError, HTTPException):
        raise MetricsError('unavailable') from None
    except (ValueError, UnicodeError):
        raise MetricsError('invalid_response') from None


def read_metrics(aid, uid, *, fetch=fetch_public):
    if not decimal_id(aid) or not decimal_id(uid):
        raise MetricsError('identity_mismatch')
    body = fetch(aid)
    if not isinstance(body, dict) or type(body.get('code')) is not int:
        raise MetricsError('invalid_response')
    if body['code'] != 0:
        raise MetricsError('rate_limited' if body['code'] in (-412, -509, -799) else 'unavailable')
    data = body.get('data')
    if not isinstance(data, dict):
        raise MetricsError('invalid_response')
    owner = data.get('owner')
    if (type(data.get('aid')) is not int or str(data['aid']) != aid
            or not isinstance(owner, dict) or type(owner.get('mid')) is not int
            or str(owner['mid']) != uid):
        raise MetricsError('identity_mismatch')
    stat = data.get('stat')
    if not isinstance(stat, dict) or type(stat.get('aid')) is not int or str(stat['aid']) != aid:
        raise MetricsError('invalid_response')
    if any(type(stat.get(key)) is not int or not 0 <= stat[key] <= 9007199254740991 for key in FIELDS):
        raise MetricsError('invalid_response')
    return {key: stat[key] for key in FIELDS}
