"""Read-only Baidu publication protocol, request scope and observed links."""
from urllib.parse import urlencode, parse_qs, urlsplit, urlunsplit
import time
import logging

logger = logging.getLogger(__name__)

from wt_media_agent.clients.baidu.identity import read_identity
from wt_media_agent.services.browser.http import ProfileHttpSession


class BaiduPublicationClient:
    """Read-only Baidu protocol and request scope; no task or browser lifecycle."""
    def __init__(self, environment, *, session_factory=ProfileHttpSession):
        self.session = session_factory(environment,
            allowed_endpoints={('baijiahao.baidu.com', '/pcui/article/lists'),
                               ('image.baidu.com', '/user/logininfo')},
            cookie_domains={'baidu.com'},
            headers={'Referer': 'https://baijiahao.baidu.com/builder/rc/content'})

    def read_identity(self):
        return read_identity(self.session.get_json)

    def find_submission(self, submission_id, *, current):
        return find_submission(self.session.get_json, submission_id, current=current)

    def close(self):
        self.session.close()


class BaiduPublicationApiError(RuntimeError):
    """The platform API did not provide a usable original-submission response."""


def list_url(number):
    return 'https://baijiahao.baidu.com/pcui/article/lists?' + urlencode({'currentPage': number, 'pageSize': 10})

def find_submission(fetch, submission_id, *, current):
    number, total = 1, None
    deadline = time.monotonic() + 60
    while current():
        if time.monotonic() >= deadline:
            raise TimeoutError("platform list read exceeded its time budget")
        response = fetch(list_url(number))
        body = response.get('json')
        if response.get('status') != 200 or not isinstance(body, dict) or type(body.get('errno')) is not int or body['errno'] != 0:
            status = response.get('status')
            errno = body.get('errno') if isinstance(body, dict) else None
            logger.info('baidu_publication_list rejected http_status=%s errno=%s json_object=%s',
                        status if type(status) is int else None,
                        errno if type(errno) is int else None, isinstance(body, dict))
            raise BaiduPublicationApiError()
        data = body.get('data')
        if not isinstance(data, dict) or not isinstance(data.get('page'), dict) or not isinstance(data.get('list'), list):
            raise ValueError('platform list shape changed')
        page, rows = data['page'], data['list']
        count = page.get('totalPage')
        if (type(page.get('currentPage')) is not int or page['currentPage'] != number
                or type(count) is not int or count < 0 or (total is not None and count != total)
                or (count < number and not (number == 1 and count == 0 and not rows))):
            raise ValueError('platform pagination changed')
        total = count
        if not rows and number == 1 and count <= 1:
            return None
        ids = [r.get('article_id') if isinstance(r, dict) else None for r in rows]
        if not rows or any(not isinstance(i, str) or not i.isascii() or not i.isdecimal() or i.startswith('0') for i in ids):
            raise ValueError('platform record identity changed')
        matches = [r for r in rows if r['article_id'] == submission_id]
        if len(matches) > 1:
            raise ValueError('ambiguous original record')
        if matches:
            return matches[0]
        if number == total:
            return None
        number += 1
    raise ValueError('publication identity changed')


def is_submission_id(value):
    return (isinstance(value, str) and 0 < len(value) <= 255
            and value.isascii() and value.isdecimal() and value[0] != '0')


def is_exact_target_url(submission_id, raw):
    if not is_submission_id(submission_id) or not isinstance(raw, str) or len(raw) > 2048:
        return False
    try:
        parsed = urlsplit(raw)
        return (parsed.scheme == 'https' and parsed.netloc == 'baijiahao.baidu.com'
                and parsed.path == '/builder/preview/s' and not parsed.fragment
                and parse_qs(parsed.query, keep_blank_values=True) == {'id': [submission_id]})
    except ValueError:
        return False


def read_public_work_id(raw):
    if not isinstance(raw, str) or len(raw) > 2048:
        return ''
    try:
        parsed = urlsplit(raw)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (parsed.scheme != 'https' or parsed.netloc != 'mbd.baidu.com'
                or parsed.path != '/newspage/data/videolanding' or parsed.fragment
                or set(query) != {'nid'} or len(query['nid']) != 1):
            return ''
        nid = query['nid'][0]
        value = nid[3:] if nid.startswith('sv_') else ''
        return value if is_submission_id(value) else ''
    except ValueError:
        return ''


def observed_target(submission_id, raw):
    if not isinstance(raw, str):
        return ''
    try:
        parsed = urlsplit(raw)
        if parsed.scheme not in {'http', 'https'}:
            return ''
        upgraded = urlunsplit(('https', parsed.netloc, parsed.path, parsed.query, parsed.fragment))
        return upgraded if is_exact_target_url(submission_id, upgraded) else ''
    except ValueError:
        return ''
