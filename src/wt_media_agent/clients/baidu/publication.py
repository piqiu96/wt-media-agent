"""Read only the observed Baidu content-list API; never manufacture URLs."""
from urllib.parse import urlencode
import time

class BrowserReadRequired(RuntimeError):
    def __init__(self, reason='api'):
        super().__init__(reason)
        self.reason = reason

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
            raise BrowserReadRequired()
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
