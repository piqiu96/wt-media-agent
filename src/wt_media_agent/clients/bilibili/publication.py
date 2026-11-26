"""Observed Bilibili creator/public protocol; read-only lookups and receipt parsing."""
from urllib.parse import urlencode

from wt_media_agent.clients.bilibili.identity import NAV_URL, identify_bilibili
from wt_media_agent.services.browser.http import ProfileHttpSession

CREATOR_ORIGIN = 'https://member.bilibili.com'
ADD_PATH = '/x/vu/web/add/v3'
TYPE_PATH = '/x/vupre/web/archive/human/type2/list'
PRE_PATH = '/x/vupre/web/archive/pre'
ARCHIVES_PATH = '/x/web/archives'
VIEW_URL = 'https://api.bilibili.com/x/web-interface/view'


class BilibiliPublicationError(ValueError):
    pass


def submission_id(value):
    if type(value) is int:
        value = str(value)
    if (not isinstance(value, str) or not value.isascii() or not value.isdecimal()
            or len(value) > 20 or int(value) <= 0 or str(int(value)) != value):
        raise BilibiliPublicationError('invalid original aid')
    return value


def work_url(aid):
    return 'https://www.bilibili.com/video/av' + submission_id(aid)


def response_data(response):
    body = response.get('json') if isinstance(response, dict) else None
    if (not isinstance(response, dict) or response.get('status') != 200 or not isinstance(body, dict)
            or type(body.get('code')) is not int or body['code'] != 0
            or not isinstance(body.get('data'), dict)):
        raise BilibiliPublicationError('platform read unavailable')
    return body['data']


def accepted_receipt(capture):
    if (not isinstance(capture, dict) or capture.get('count') != 1
            or capture.get('forwarded') != 1):
        raise BilibiliPublicationError('no unique authorized submission')
    data = response_data(capture)
    return {'platform_work_id': submission_id(data.get('aid'))}


def matching_partition(data, partition_id, name):
    rows = data.get('type_list')
    if not isinstance(rows, list):
        raise BilibiliPublicationError('partition catalog unavailable')
    matches = [r for r in rows if isinstance(r, dict) and type(r.get('id')) is int
               and r['id'] == partition_id and r.get('name') == name]
    if len(matches) != 1:
        raise BilibiliPublicationError('confirmed partition no longer matches platform catalog')
    return matches[0]


def archives_url(page):
    if type(page) is not int or not 1 <= page <= 1000:
        raise BilibiliPublicationError('invalid archive page')
    return CREATOR_ORIGIN + ARCHIVES_PATH + '?' + urlencode({
        'status': 'is_pubing,pubed,not_pubed', 'pn': page, 'ps': 10,
        'coop': 1, 'interactive': 1, 'client_type': 1,
    })


def find_submission(fetch, aid, *, current=lambda: True):
    aid = submission_id(aid)
    count = None
    seen = set()
    found = None
    for page in range(1, 1001):
        if not current():
            raise BilibiliPublicationError('check authorization changed')
        data = response_data(fetch(archives_url(page)))
        paging, rows = data.get('page'), data.get('arc_audits')
        if (not isinstance(paging, dict) or paging.get('pn') != page or paging.get('ps') != 10
                or type(paging.get('count')) is not int or not 0 <= paging['count'] <= 10000
                or not isinstance(rows, list) or len(rows) > 10):
            raise BilibiliPublicationError('archive pagination changed')
        if count is None:
            count = paging['count']
        if count != paging['count'] or len(rows) != min(10, max(0, count - (page - 1) * 10)):
            raise BilibiliPublicationError('archive list changed during read')
        for item in rows:
            row = item.get('Archive') if isinstance(item, dict) else None
            if not isinstance(row, dict):
                raise BilibiliPublicationError('archive record changed')
            row_id = submission_id(row.get('aid'))
            if row_id in seen:
                raise BilibiliPublicationError('duplicate archive identity')
            seen.add(row_id)
            if row_id == aid:
                found = row
        if page * 10 >= count:
            return found
    raise BilibiliPublicationError('archive pagination exceeds bounded read')


class BilibiliPublicationClient:
    def __init__(self, environment, *, session_factory=ProfileHttpSession):
        self.session = session_factory(environment, allowed_endpoints={
            ('api.bilibili.com', '/x/web-interface/nav'),
            ('api.bilibili.com', '/x/web-interface/view'),
            ('member.bilibili.com', ARCHIVES_PATH),
        }, cookie_domains={'bilibili.com'}, headers={'Referer': CREATOR_ORIGIN + '/'})

    def read_identity(self):
        response = self.session.get_json(NAV_URL)
        return identify_bilibili([], response.get('json') if response.get('status') == 200 else None)

    def find_submission(self, aid, *, current=lambda: True):
        return find_submission(self.session.get_json, aid, current=current)

    def close(self):
        self.session.close()
