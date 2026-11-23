"""Read one exact original Baidu submission; never upload, edit or submit."""
from datetime import datetime, timezone
from math import isfinite

from wt_media_agent.clients.baidu.publication import (
    is_submission_id, is_exact_target_url, read_public_work_id, observed_target,
    find_submission, list_url, BaiduPublicationClient, BaiduPublicationApiError,
)
from wt_media_agent.platforms.baidu.identity import read_baidu_identity
from wt_media_agent.platforms.publication_check import PublicationCheckError, PublicationReference, PublicWorkObservation, PublicationCheckControl
from wt_media_agent.services.browser.http import ProfileHttpError
from wt_media_agent.services.browser.session import BrowserSession


def verify_public_metadata(work_url, expected_description, metadata, *, now):
    if (not isinstance(metadata, dict) or metadata.get('id') != read_public_work_id(work_url)
            or not read_public_work_id(work_url) or metadata.get('title') != expected_description
            or metadata.get('media_ready') is not True or type(metadata.get('duration')) not in (int, float)
            or not isfinite(metadata['duration']) or metadata['duration'] <= 0 or type(metadata.get('publish_time')) is not int):
        return None
    stamp = metadata['publish_time']
    if stamp <= 0 or stamp > int(now.timestamp()):
        return None
    try:
        return datetime.fromtimestamp(stamp, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _read_only(route):
    if route.request.method in {'GET', 'HEAD', 'OPTIONS'}:
        route.fallback()
    else:
        route.abort()


# Read only the observed public data shape, and return only proof fields.
# A recommendation, logged-in preview or arbitrary HTTP 200 cannot satisfy it.
PUBLIC_METADATA = """expectedId => {
 const roots=Object.values(Object.getOwnPropertyDescriptors(window))
   .filter(d => {
     if(!('value' in d) || !d.value || typeof d.value!=='object')return false;
     try{return Object.prototype.hasOwnProperty.call(d.value,'curVideoMeta');}catch{return false;}
   });
 const values=roots.map(d=>d.value.curVideoMeta).filter(v=>v && v.id===expectedId);
 if(values.length!==1)return false;
 const m=values[0];
 const media=[...document.querySelectorAll('video')].filter(v=>
   typeof m.playurl==='string' && m.playurl.startsWith('https://')
   && v.currentSrc===m.playurl && v.readyState>=2
   && v.videoWidth>0 && v.videoHeight>0
   && Number.isFinite(v.duration) && v.duration>0 && Math.abs(v.duration-m.duration)<2);
 if(media.length!==1)return false;
 return {id:m.id,title:m.title,publish_time:m.publish_time,duration:m.duration,media_ready:true};
}"""


class _BrowserReadRequired(RuntimeError):
    def __init__(self, reason='api'):
        super().__init__(reason)
        self.reason = reason


class BaiduPublicationChecker:
    def __init__(self, *, client_factory=BaiduPublicationClient, browser_session_factory=BrowserSession, identity_reader=None):
        self.client_factory = client_factory
        self.endpoint = None
        self.browser_session_factory = browser_session_factory
        self.identity_reader = identity_reader

    def check(self, reference: PublicationReference, control: PublicationCheckControl) -> PublicWorkObservation:
        if not control.current():
            raise PublicationCheckError('publication check identity changed')
        if not is_submission_id(reference.submission_id):
            raise PublicationCheckError('original submission identity is missing')
        try:
            return self._read_api(reference, control)
        except _BrowserReadRequired as required:
            public_proof = required.reason == 'public_proof'
        except BaiduPublicationApiError:
            public_proof = False
        if not control.browser_fallback_allowed and not public_proof:
            return PublicWorkObservation('uncertain', '接口读取需人工核对，已停止自动核验',
                '本机接口仍不可用，原任务已经尝试过浏览器接口兜底，不再反复打开窗口', lookup_result='blocked')
        if not control.current():
            raise PublicationCheckError('publication check identity changed')
        with control.browser() as endpoint:
            self.endpoint = endpoint
            observation = self._read_browser(reference, current=control.current)
        if public_proof and observation.conclusion == 'reviewing':
            return PublicWorkObservation('uncertain', '指定作品公开依据尚未齐全，请人工核实',
                '列表接口已显示发布，但匿名可播放媒体或实际发布时间未能完整核实；停止反复开窗，保留审核中', lookup_result='blocked')
        return observation

    def _read_api(self, reference, control):
        current = control.current
        try:
            client = self.client_factory(control.read_environment())
        except ProfileHttpError:
            raise _BrowserReadRequired() from None
        try:
            if not current():
                raise PublicationCheckError('publication identity changed')
            identity = client.read_identity()
            if identity['login_status'] != 'normal':
                raise _BrowserReadRequired()
            uid = identity['platform_account_id']
            if uid != reference.platform_uid:
                return PublicWorkObservation('uncertain', '账号身份不一致，请核对原账号',
                    '当前登录态与原投稿账号UID不一致，停止自动核验，不上传或重新投稿', lookup_result='blocked')
            row = client.find_submission(reference.submission_id, current=current)
            if not current():
                raise PublicationCheckError('publication identity changed')
            if row is None:
                return PublicWorkObservation('uncertain', '原账号内容列表未找到指定投稿',
                    '已核对原账号并完成列表接口实际分页，没有找到原投稿；不据此判定失败或重新投稿', lookup_result='not_found')
            target = observed_target(reference.submission_id, row.get('url'))
            if reference.target_url and target and reference.target_url != target:
                raise PublicationCheckError('original target changed')
            if row.get('is_published') == 1 and row.get('status') == 'publish':
                raise _BrowserReadRequired('public_proof')
            return PublicWorkObservation('reviewing', '平台已接收，等待指定作品公开',
                '原账号UID及列表接口原投稿ID匹配，公开媒体和实际发布时间尚待完整核验',
                reference.submission_id, target or reference.target_url, lookup_result='found')
        finally:
            client.close()

    def _verify_owner(self, reference, current, *, page=None):
        if not current():
            raise PublicationCheckError('publication check identity changed')
        if self.identity_reader is not None:
            observed = self.identity_reader(self.endpoint)
        elif page is not None:
            observed = read_baidu_identity(self.endpoint, browser_context=page.context)
        else:
            observed = read_baidu_identity(self.endpoint, browser_session_factory=self.browser_session_factory)
        if (observed.get('login_status') != 'normal'
                or observed.get('platform_account_id') != reference.platform_uid):
            raise PublicationCheckError('publication check live account differs')

    def _locate_target(self, page, reference, current):
        if reference.target_url:
            if not is_exact_target_url(reference.submission_id, reference.target_url):
                raise PublicationCheckError('original target differs from receipt')
            return reference.target_url
        # Visit JSON once to establish the platform origin. All pagination is GET fetch,
        # never navigation to the work-list UI or a click on a pagination control.
        response = page.goto(list_url(1), wait_until='domcontentloaded', timeout=20000)
        first = {'status': response.status, 'json': response.json()} if response else {'status': 0, 'json': None}
        def fetch(url):
            if url == list_url(1):
                return first
            return page.evaluate("""async url => {
                const r=await fetch(url,{credentials:'include',redirect:'error'});
                let body=null;try{body=await r.json();}catch{}
                return {status:r.status,json:body};
            }""", url)
        try:
            row = find_submission(fetch, reference.submission_id, current=current)
        except ValueError:
            raise PublicationCheckError('original submission list changed') from None
        if row is None:
            return None
        target = observed_target(reference.submission_id, row.get('url'))
        if not target:
            raise PublicationCheckError('original record has no verified target link')
        return target

    def _anonymous_proof(self, browser, work_url, description):
        from playwright.sync_api import TimeoutError as BrowserTimeout
        anonymous = browser.new_context(storage_state={'cookies': [], 'origins': []}, service_workers='block')
        try:
            page = anonymous.new_page()
            page.route('**/*', _read_only)
            response = page.goto(work_url, wait_until='domcontentloaded', timeout=20000)
            if response is None or response.status != 200 or page.url != work_url:
                return None
            try:
                metadata = page.wait_for_function(PUBLIC_METADATA, arg=read_public_work_id(work_url), timeout=15000).json_value()
            except BrowserTimeout:
                return None
            return verify_public_metadata(work_url, description, metadata, now=datetime.now(timezone.utc))
        finally:
            anonymous.close()

    def _read_browser(self, reference, *, current):
        if not is_submission_id(reference.submission_id):
            raise PublicationCheckError('original submission identity is missing')
        description = reference.content.get('description')
        topics = reference.content.get('topics', '')
        if not isinstance(description, str) or not description.strip() or not isinstance(topics, str):
            raise PublicationCheckError('original confirmed publication text is missing')
        description += (' ' + topics if topics else '')
        self._verify_owner(reference, current)
        with self.browser_session_factory(self.endpoint) as session:
            page = session.page
            page.route('**/*', _read_only)
            try:
                target = self._locate_target(page, reference, current)
                self._verify_owner(reference, current, page=page)
                if target is None:
                    return PublicWorkObservation('uncertain', '原账号内容列表未找到指定投稿',
                        '已按平台实际分页查找原投稿回执，暂未找到对应记录；不能据此认定投稿失败，不重新上传或投稿，需核对原回执与平台记录', lookup_result='not_found')
                response = page.goto(target, wait_until='domcontentloaded', timeout=20000)
                work_url = page.url
                work_id = read_public_work_id(work_url)
                published_at = None
                if response is not None and response.status == 200 and work_id:
                    published_at = self._anonymous_proof(page.context.browser, work_url, description)
                self._verify_owner(reference, current, page=page)
                if published_at is not None:
                    return PublicWorkObservation('published', '指定作品已经公开',
                        '原账号UID核对一致；按原投稿ID的实际链接跳转，匿名页面作品ID、确认内容、可加载媒体及平台publish_time一致',
                        reference.submission_id, target, work_id, work_url, published_at, lookup_result="found")
                return PublicWorkObservation('reviewing', '等待指定作品公开',
                    '原账号UID及指定投稿实际链接已核对，匿名可加载媒体、确认内容或平台发布时间证据尚未齐全，保留平台审核中',
                    reference.submission_id, target, lookup_result="found")
            finally:
                page.close()
