"""API-first original-submission lookup using the account's local HTTP environment."""
from wt_media_agent.clients.baidu.publication import BaiduPublicationClient, BrowserReadRequired, observed_target
from wt_media_agent.services.browser.http import ProfileHttpError
from wt_media_agent.platforms.publish import PublicWorkObservation, PublicationInputError


class BaiduPublicationApiReader:
    def __init__(self, environment, *, client_factory=BaiduPublicationClient):
        self.environment = environment
        self.client_factory = client_factory

    def read(self, reference, *, current):
        try:
            client = self.client_factory(self.environment)
        except ProfileHttpError:
            raise BrowserReadRequired() from None
        try:
            if not current():
                raise PublicationInputError('publication identity changed')
            identity = client.read_identity()
            if identity['login_status'] != 'normal':
                raise BrowserReadRequired()
            uid = identity['platform_account_id']
            if uid != reference.platform_uid:
                return PublicWorkObservation('uncertain', '账号身份不一致，请核对原账号',
                    '当前登录态与原投稿账号UID不一致，停止自动核验，不上传或重新投稿', lookup_result='blocked')
            row = client.find_submission(reference.submission_id, current=current)
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
            client.close()
