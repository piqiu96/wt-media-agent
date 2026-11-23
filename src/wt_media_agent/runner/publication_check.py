"""Serial, owner-bound public-result reads. No publication attempt or file grant."""
from __future__ import annotations

import logging
from datetime import timezone
from dataclasses import replace
from pathlib import Path
from typing import Mapping

from wt_media_agent.clients.bitbrowser.errors import BitBrowserUnavailableError
from wt_media_agent.clients.baidu.publication import BrowserReadRequired
from wt_media_agent.platforms.publish import PublicationInputError, PublicationReference, PublicWorkObservation

logger = logging.getLogger(__name__)


def _identity(value):
    return (isinstance(value, str) and 0 < len(value) <= 20 and value.isascii()
            and value.isdecimal() and str(int(value)) == value
            and 0 < int(value) <= 18446744073709551615)


class PublicationCheckJob:
    def __init__(self, cloud, bitbrowser, journal, reader_factory, *, api_reader_factory=None):
        self.cloud = cloud
        self.bitbrowser = bitbrowser
        self.profile_operations = journal.profile_operations
        self.reader_factory = reader_factory
        self.api_reader_factory = api_reader_factory

    def _claim_identity(self, claim, user):
        if not isinstance(claim, Mapping):
            raise ValueError('invalid publication check claim')
        task, attempt = claim.get('task', {}), claim.get('attempt', {})
        if not isinstance(task, Mapping) or not isinstance(attempt, Mapping):
            raise ValueError('invalid publication check task or attempt')
        source = attempt.get('input_snapshot', {})
        if not isinstance(source, Mapping):
            raise ValueError('invalid original publication check input')
        identities = (task.get('id'), attempt.get('id'), attempt.get('profile_id'),
                      source.get('profile_id'), claim.get('generation'), claim.get('expected_revision'))
        if (not all(_identity(value) for value in identities)
                or task.get('operator_id') != user or attempt.get('executor_user_id') != user
                or attempt.get('publication_id') != task['id'] or task.get('current_attempt_id') != attempt['id']
                or task.get('status') != 'reviewing' or task.get('platform') != 'baidu'
                or task.get('revision') != claim['expected_revision']
                or attempt['profile_id'] != source['profile_id']
                or not isinstance(source.get('platform_uid'), str) or not source['platform_uid']
                or source.get('platform_uid') != task.get('platform_account_uid')
                or not isinstance(task.get('platform_submission_id'), str)
                or not 0 < len(task['platform_submission_id']) <= 255
                or not task['platform_submission_id'].isascii() or not task['platform_submission_id'].isdecimal()
                or task['platform_submission_id'][0] == '0'
                or not isinstance(source.get('bit_profile_id'), str) or not source['bit_profile_id']
                or not isinstance(source.get('bit_main_user_id'), str) or not source['bit_main_user_id']
                or not isinstance(source.get('content'), Mapping)):
            raise ValueError('publication check differs from original owner or attempt')
        return task, attempt, source

    def tick(self, token, user, *, current):
        if not current():
            return
        claim = self.cloud.claim_publication_check(token)
        if claim is None or not current():
            return
        task, attempt, source = self._claim_identity(claim, user)
        observation = PublicWorkObservation('uncertain', '暂未完成指定稿件核验',
            '原账号现场或平台读取尚未取得完整证据；本轮未上传或投稿，保留平台审核中')
        closed = False
        profile_id = source['bit_profile_id']
        endpoint = ''
        browser_unavailable = False
        browser_used = False
        public_proof = False
        try:
            with self.profile_operations.acquire(profile_id):
                profiles = self.bitbrowser.scan_profiles().profiles
                owned = [p for p in profiles if p.bit_profile_id == profile_id and p.main_user_id == source['bit_main_user_id']]
                if len(owned) != 1 or not current():
                    raise PublicationInputError('original check Profile owner differs')
                reference = PublicationReference(source['platform_uid'], task['platform_submission_id'],
                    dict(source['content']), task.get('target_url') or '')
                needs_browser = self.api_reader_factory is None
                if self.api_reader_factory is not None:
                    if not self.bitbrowser.is_profile_closed(profile_id):
                        raise PublicationInputError('original Profile is occupied')
                    closed = True  # Verified absence of a browser scene, not an invented close.
                    try:
                        reader = self.api_reader_factory(task['platform'])(profile_id)
                        observation = reader.read(reference, current=current)
                    except BrowserReadRequired as required:
                        needs_browser = True
                        public_proof = required.reason == 'public_proof'
                        if not claim.get('browser_fallback_allowed', True) and required.reason != 'public_proof':
                            observation = PublicWorkObservation('uncertain', '接口读取需人工核对，已停止自动核验',
                                '本机接口仍不可用，原任务已经尝试过浏览器接口兜底，不再反复打开窗口', lookup_result='blocked')
                            needs_browser = False
                if needs_browser:
                    # Opening a new scene invalidates the earlier absence check,
                    # including when the open or close reply is lost.
                    closed = False
                    try:
                        endpoint = self.bitbrowser.open_profile_for_check(profile_id)
                        browser_used = True
                        if not current():
                            raise PublicationInputError('check identity changed')
                        reader = self.reader_factory(task['platform'])(endpoint)
                        observation = reader.read(reference, current=current)
                    finally:
                        if endpoint:
                            closed = self.bitbrowser.close_profile_verified(profile_id, endpoint) is True
                if self.api_reader_factory is not None and not endpoint:
                    closed = self.bitbrowser.is_profile_closed(profile_id)
                if not isinstance(observation, PublicWorkObservation) or observation.conclusion not in {'uncertain','reviewing','published'}:
                    raise PublicationInputError('unsupported public observation')
        except Exception as error:
            location = error.__traceback__
            failure_path = []
            while location is not None:
                code = location.tb_frame.f_code
                if 'wt_media_agent' in Path(code.co_filename).parts:
                    failure_path.append(f'{Path(code.co_filename).name}:{code.co_name}:{location.tb_lineno}')
                if location.tb_next is None:
                    break
                location = location.tb_next
            # Keep source locations only; exception text can contain platform secrets.
            code = location.tb_frame.f_code
            logger.info('publication check deferred error_type=%s failure_site=%s:%s:%s failure_path=%s',
                type(error).__name__, Path(code.co_filename).name, code.co_name,
                location.tb_lineno, '>'.join(failure_path))
            browser_unavailable = isinstance(error, BitBrowserUnavailableError)
            observation = PublicWorkObservation('uncertain', '暂未完成指定稿件核验',
                '原账号现场或平台读取尚未取得完整证据；本轮未上传或投稿，保留平台审核中', lookup_result='blocked' if browser_used else '')
        if not current():
            return
        if browser_unavailable:
            observation = PublicWorkObservation('uncertain', '比特浏览器服务不可达，请启动并检查本地API',
                '本轮未完成原账号现场核验与安全收尾，未上传或投稿；服务恢复后继续核验原稿件')
        elif not closed:
            observation = PublicWorkObservation('uncertain', '等待原账号现场可用',
                '未取得仅供本轮核验使用且已安全关闭的原账号现场，保持原任务与原结果，不上传或投稿')
        if browser_used and observation.conclusion == 'uncertain' and observation.lookup_result != 'not_found':
            observation = replace(observation, lookup_result='blocked')
        if public_proof and browser_used and observation.conclusion == 'reviewing':
            observation = PublicWorkObservation('uncertain', '指定作品公开依据尚未齐全，请人工核实',
                '列表接口已显示发布，但匿名可播放媒体或实际发布时间未能完整核实；停止反复开窗，保留审核中', lookup_result='blocked')
        public = observation.conclusion == 'published'
        value = {'attempt_id': attempt['id'], 'expected_revision': claim['expected_revision'],
            'conclusion': observation.conclusion, 'evidence': {
                'platform_uid': source['platform_uid'], 'reason': observation.reason, 'basis': observation.basis,
                'scene_closed': closed, 'work_matched': observation.conclusion in {'reviewing', 'published'},
                'publicly_accessible': public, 'no_publishable_work': False}}
        if observation.conclusion in {'reviewing', 'published'}:
            value.update(platform_submission_id=observation.submission_id, target_url=observation.target_url)
        if public:
            if observation.published_at is None:
                raise PublicationInputError('public check has no platform publication time')
            value.update(platform_work_id=observation.work_id, work_url=observation.work_url,
                published_at=observation.published_at.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'))
        report = {'generation': claim['generation'], 'observation': value}
        if observation.lookup_result:
            report['lookup_result'] = observation.lookup_result
        if browser_used:
            report['browser_used'] = True
        self.cloud.observe_publication(token, task['id'], report)
