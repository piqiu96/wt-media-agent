"""Read one exact original Baidu submission; never upload, edit or submit."""
from datetime import datetime, timezone
from math import isfinite
from urllib.parse import parse_qs, urlsplit

from wt_media_agent.platforms.baidu.publisher import publication_description, read_baidu_identity
from wt_media_agent.platforms.publish import PublicationInputError, PublicationReference, PublicWorkObservation
from wt_media_agent.services.browser.session import BrowserSession


def _decimal(value):
    return (isinstance(value, str) and 0 < len(value) <= 255
            and value.isascii() and value.isdecimal() and value[0] != '0')


def is_exact_target_url(submission_id, raw):
    if not _decimal(submission_id) or not isinstance(raw, str) or len(raw) > 2048:
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
        return value if _decimal(value) else ''
    except ValueError:
        return ''


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


class BaiduPublicationReader:
    def __init__(self, endpoint, *, browser_session_factory=BrowserSession, identity_reader=read_baidu_identity):
        self.endpoint = endpoint
        self.browser_session_factory = browser_session_factory
        self.identity_reader = identity_reader

    def _verify_owner(self, reference, current):
        if not current():
            raise PublicationInputError('publication check identity changed')
        observed = self.identity_reader(self.endpoint)
        if (observed.get('login_status') != 'normal'
                or observed.get('platform_account_id') != reference.platform_uid):
            raise PublicationInputError('publication check live account differs')

    def _locate_target(self, page, reference):
        if reference.target_url:
            if not is_exact_target_url(reference.submission_id, reference.target_url):
                raise PublicationInputError('original target differs from receipt')
            return reference.target_url
        page.goto('https://baijiahao.baidu.com/builder/rc/content', wait_until='domcontentloaded')
        found = page.wait_for_function("""id => {
          const links=[...document.querySelectorAll('a[href]')].map(a=>a.href).filter(raw=>{
            const u=new URL(raw);
            return u.protocol==='https:' && u.host==='baijiahao.baidu.com'
              && u.pathname==='/builder/preview/s' && !u.hash
              && [...u.searchParams].length===1 && u.searchParams.get('id')===id;
          });
          const exact=[...new Set(links)];return exact.length===1 ? exact[0] : false;
        }""", arg=reference.submission_id, timeout=15000).json_value()
        if not is_exact_target_url(reference.submission_id, found):
            raise PublicationInputError('no unique original submission link')
        return found

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

    def read(self, reference, *, current):
        if not _decimal(reference.submission_id):
            raise PublicationInputError('original submission identity is missing')
        description = publication_description(reference.content)
        self._verify_owner(reference, current)
        with self.browser_session_factory(self.endpoint) as session:
            page = session.page
            page.route('**/*', _read_only)
            try:
                target = self._locate_target(page, reference)
                self._verify_owner(reference, current)
                response = page.goto(target, wait_until='domcontentloaded', timeout=20000)
                work_url = page.url
                work_id = read_public_work_id(work_url)
                published_at = None
                if response is not None and response.status == 200 and work_id:
                    published_at = self._anonymous_proof(page.context.browser, work_url, description)
                self._verify_owner(reference, current)
                if published_at is not None:
                    return PublicWorkObservation('published', '指定作品已经公开',
                        '原账号UID核对一致；按原投稿ID的实际链接跳转，匿名页面作品ID、确认内容、可加载媒体及平台publish_time一致',
                        reference.submission_id, target, work_id, work_url, published_at)
                return PublicWorkObservation('reviewing', '等待指定作品公开',
                    '原账号UID及指定投稿实际链接已核对，匿名可加载媒体、确认内容或平台发布时间证据尚未齐全，保留平台审核中',
                    reference.submission_id, target)
            finally:
                page.close()
