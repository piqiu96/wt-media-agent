"""API-first original-submission lookup using the account's local HTTP environment."""
import time
from urllib.parse import urlsplit, urlunsplit
from wt_media_agent.clients.baidu.publication import find_submission, BrowserReadRequired
from wt_media_agent.services.browser.http import ProfileHttpSession, ProfileHttpError
from wt_media_agent.platforms.publish import PublicWorkObservation, PublicationInputError
from wt_media_agent.platforms.baidu.publication_check import is_exact_target_url


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

class BaiduPublicationApiReader:
    def __init__(self, environment, *, session_factory=ProfileHttpSession):
        self.environment = environment
        self.session_factory = session_factory

    def read(self, reference, *, current):
        try:
            session = self.session_factory(self.environment)
        except ProfileHttpError:
            raise BrowserReadRequired() from None
        try:
            if not current():
                raise PublicationInputError('publication identity changed')
            identity = session.get_json('https://image.baidu.com/user/logininfo?time=' + str(int(time.time()*1000)) + '&src=pc&page=index')
            value = identity.get('json') or {}
            uid = str(((value.get('data') or {}).get('user') or {}).get('user_id') or '')
            if identity.get('status') != 200 or not uid:
                raise BrowserReadRequired()
            if uid != reference.platform_uid:
                return PublicWorkObservation('uncertain', '账号身份不一致，请核对原账号',
                    '当前登录态与原投稿账号UID不一致，停止自动核验，不上传或重新投稿', lookup_result='blocked')
            row = find_submission(session.get_json, reference.submission_id, current=current)
            if not current():
                raise PublicationInputError('publication identity changed')
            if row is None:
                return PublicWorkObservation('uncertain', '原账号内容列表未找到指定投稿',
                    '已核对原账号并完成列表接口实际分页，没有找到原投稿；不据此判定失败或重新投稿', lookup_result='not_found')
            target = observed_target(reference.submission_id, row.get('url'))
            if reference.target_url and target and reference.target_url != target:
                raise PublicationInputError('original target changed')
            if row.get('is_published') == 1 and row.get('status') == 'publish':
                raise BrowserReadRequired('public_proof')
            return PublicWorkObservation('reviewing', '平台已接收，等待指定作品公开',
                '原账号UID及列表接口原投稿ID匹配，公开媒体和实际发布时间尚待完整核验',
                reference.submission_id, target or reference.target_url, lookup_result='found')
        finally:
            session.close()
