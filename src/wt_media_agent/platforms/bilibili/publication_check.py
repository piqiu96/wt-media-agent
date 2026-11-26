"""Exact original aid + owner readback, followed by anonymous playable proof."""
from datetime import datetime, timezone
from math import isfinite
from urllib.parse import urlsplit

from wt_media_agent.clients.bilibili.publication import (
    BilibiliPublicationClient, BilibiliPublicationError, VIEW_URL,
    find_submission, response_data, submission_id, work_url,
)
from wt_media_agent.platforms.bilibili.identity import read_bilibili_identity
from wt_media_agent.platforms.publication_check import (
    PublicationCheckError, PublicWorkObservation,
)
from wt_media_agent.services.browser.http import ProfileHttpError
from wt_media_agent.services.browser.session import BrowserSession


def _read_only(route):
    if route.request.method in {'GET', 'HEAD', 'OPTIONS'}:
        route.fallback()
    else:
        route.abort()


def verify_metadata(reference, data, *, now):
    try:
        if (submission_id(data.get('aid')) != reference.submission_id
                or submission_id(data.get('owner', {}).get('mid')) != reference.platform_uid
                or data.get('title') != reference.content.get('title')
                or type(data.get('state')) is not int or data['state'] != 0
                or type(data.get('pubdate')) is not int
                or not 0 < data['pubdate'] <= int(now.timestamp())
                or type(data.get('duration')) not in (int, float)
                or not isfinite(data['duration']) or data['duration'] <= 0
                or type(data.get('cid')) is not int or data['cid'] <= 0
                or not isinstance(data.get('bvid'), str) or not data['bvid'].startswith('BV')):
            return None
        return datetime.fromtimestamp(data['pubdate'], tz=timezone.utc)
    except (BilibiliPublicationError, AttributeError, TypeError, ValueError, OverflowError, OSError):
        return None


PUBLIC_MEDIA = """expected => {
 const m=window.__INITIAL_STATE__?.videoData;
 if(!m || String(m.aid)!==expected.aid || m.bvid!==expected.bvid
    || String(m.owner?.mid)!==expected.uid || m.title!==expected.title
    || Number(m.cid)!==expected.cid || Number(m.pubdate)!==expected.pubdate)return false;
 const media=[...document.querySelectorAll('video')].filter(v=>{
   const r=v.getBoundingClientRect();return r.width>0 && r.height>0
    && v.readyState>=2 && v.videoWidth>0 && v.videoHeight>0
    && Number.isFinite(v.duration) && Math.abs(v.duration-expected.duration)<2;
 });
 return media.length===1;
}"""


class BilibiliPublicationChecker:
    def __init__(self, *, client_factory=BilibiliPublicationClient,
                 browser_session_factory=BrowserSession, identity_reader=None):
        self.client_factory = client_factory
        self.browser_session_factory = browser_session_factory
        self.identity_reader = identity_reader

    def _verify_owner(self, endpoint, reference, current, page):
        if not current():
            raise PublicationCheckError('publication check authorization changed')
        identity = (self.identity_reader(endpoint) if self.identity_reader else
                    read_bilibili_identity(endpoint, browser_context=page.context))
        if identity.get('login_status') != 'normal' or identity.get('platform_account_id') != reference.platform_uid:
            raise PublicationCheckError('original account identity differs')

    def _observation(self, reference, row):
        if row is None:
            return PublicWorkObservation('uncertain', '原账号未找到指定稿件，请核实原回执',
                '已完成平台实际分页，未按标题或最新作品替代原稿件；保留占用，不重新投稿', lookup_result='not_found')
        if (submission_id(row.get('mid')) != reference.platform_uid
                or row.get('title') != reference.content.get('title')):
            raise PublicationCheckError('original archive owner or title differs')
        if type(row.get('state')) is not int:
            raise BilibiliPublicationError('original archive state unavailable')
        attrs = row.get('attrs')
        if not isinstance(attrs, dict) or attrs.get('is_owner') != 1:
            raise BilibiliPublicationError('original archive ownership unavailable')
        if attrs.get('no_public') != 0 or reference.content.get('bilibili', {}).get('visibility') != 'public':
            return PublicWorkObservation('uncertain', '稿件非公开可见，不能计为公开成功',
                '原账号和原稿件已匹配，可见范围不满足公开结果要求', lookup_result='blocked')
        if row['state'] == 0 and row.get('state_desc') == '开放浏览':
            return None  # Requires an independent anonymous media proof.
        state = row.get('state_desc', '')
        rejected = row.get('reject_reason')
        if rejected or any(word in state for word in ('退回', '未通过', '锁定', '删除', '不适宜')):
            reason = str(rejected or state).replace('\r', ' ').replace('\n', ' ')[:200]
            return PublicWorkObservation('uncertain', '原稿件未通过，请到原任务处理',
                '原账号和原稿件已匹配；平台反馈：' + reason + '；不重新投稿', lookup_result='blocked')
        if any(word in state for word in ('审核', '转码', '处理中')):
            return PublicWorkObservation('reviewing', '原稿件仍在平台处理中',
                '原账号、稿件ID和标题已匹配；平台状态：' + state[:100],
                reference.submission_id, work_url(reference.submission_id), lookup_result='found')
        return PublicWorkObservation('uncertain', '原稿件状态需人工核实',
            '原账号和稿件ID已匹配，但平台状态尚不能证明公开；不重新投稿', lookup_result='blocked')

    def check(self, reference, control):
        try:
            submission_id(reference.submission_id)
            submission_id(reference.platform_uid)
        except BilibiliPublicationError:
            raise PublicationCheckError('original submission identity is missing') from None
        target = work_url(reference.submission_id)
        if reference.target_url and reference.target_url.rstrip('/') != target:
            raise PublicationCheckError('original target changed')
        if not isinstance(reference.content.get('title'), str) or not reference.content['title'].strip():
            raise PublicationCheckError('original title is missing')
        if not control.current():
            raise PublicationCheckError('publication check authorization changed')
        need_public = False
        try:
            client = self.client_factory(control.read_environment())
            try:
                identity = client.read_identity()
                if identity.get('login_status') != 'normal':
                    raise BilibiliPublicationError('login unavailable')
                if identity.get('platform_account_id') != reference.platform_uid:
                    raise PublicationCheckError('original account identity differs')
                row = client.find_submission(reference.submission_id, current=control.current)
                if not control.current():
                    raise PublicationCheckError('publication check authorization changed')
                observation = self._observation(reference, row)
                if observation is not None:
                    return observation
                need_public = True
            finally:
                client.close()
        except (ProfileHttpError, BilibiliPublicationError):
            if not control.browser_fallback_allowed:
                return PublicWorkObservation('uncertain', '原稿件接口读取受阻，请人工核实',
                    '未取得可信原稿件依据，保留占用，不上传或重新投稿', lookup_result='blocked')
        with control.browser() as endpoint:
            with self.browser_session_factory(endpoint) as session:
                page = session.page
                page.route('**/*', _read_only)
                try:
                    self._verify_owner(endpoint, reference, control.current, page)
                    if not need_public:
                        def fetch(url):
                            if not control.current():
                                raise PublicationCheckError('publication check authorization changed')
                            r = page.goto(url, wait_until='domcontentloaded', timeout=20000)
                            return {'status': r.status if r else 0, 'json': r.json() if r and r.status == 200 else None}
                        observation = self._observation(reference, find_submission(fetch, reference.submission_id, current=control.current))
                        if observation is not None:
                            return observation
                    stamp = self._anonymous_proof(page.context.browser, reference)
                    self._verify_owner(endpoint, reference, control.current, page)
                    if stamp:
                        return PublicWorkObservation('published', '原稿件已公开且可匿名播放',
                            '原账号UID、原aid、标题及匿名页面媒体、CID和实际发布时间一致',
                            reference.submission_id, target, reference.submission_id, target, stamp, lookup_result='found')
                    return PublicWorkObservation('uncertain', '公开播放依据尚未齐全，请核实原稿件',
                        '平台后台显示开放浏览，但匿名媒体证据不完整；保留占用，不重新投稿', lookup_result='blocked')
                finally:
                    page.close()

    def _anonymous_proof(self, browser, reference):
        from playwright.sync_api import TimeoutError as BrowserTimeout
        anonymous = browser.new_context(storage_state={'cookies': [], 'origins': []}, service_workers='block')
        try:
            page = anonymous.new_page()
            page.route('**/*', _read_only)
            response = page.goto(VIEW_URL + '?aid=' + reference.submission_id, wait_until='domcontentloaded', timeout=20000)
            data = response_data({'status': response.status if response else 0,
                                  'json': response.json() if response and response.status == 200 else None})
            stamp = verify_metadata(reference, data, now=datetime.now(timezone.utc))
            if not stamp:
                return None
            response = page.goto(work_url(reference.submission_id), wait_until='domcontentloaded', timeout=20000)
            location = urlsplit(page.url)
            if (response is None or response.status != 200 or location.scheme != 'https'
                    or location.netloc != 'www.bilibili.com' or location.query or location.fragment
                    or location.path.rstrip('/') not in ('/video/av' + reference.submission_id, '/video/' + data['bvid'])):
                return None
            try:
                page.wait_for_function(PUBLIC_MEDIA, arg={'aid': reference.submission_id,
                    'uid': reference.platform_uid, 'bvid': data['bvid'], 'cid': data['cid'],
                    'title': reference.content['title'], 'duration': data['duration'], 'pubdate': data['pubdate']}, timeout=20000)
                return stamp
            except BrowserTimeout:
                return None
        except BilibiliPublicationError:
            return None
        finally:
            anonymous.close()
