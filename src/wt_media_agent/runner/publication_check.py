"""Durable original-check recovery. No retry authorizes a second browser open."""
from __future__ import annotations
import logging
from datetime import timezone
from contextlib import nullcontext
from pathlib import Path
from threading import Lock
from typing import Mapping
from wt_media_agent.platforms.publication_check import PublicationCheckError, PublicationReference, PublicWorkObservation, PublicationCheckControl
from wt_media_agent.services.browser.session import BrowserSession, ProfileReadSession
from wt_media_agent.storage.publication_check import PublicationCheckStore
from wt_media_agent.clients.cloud.errors import CloudError, StaleAttemptError

logger = logging.getLogger(__name__)

def _identity(value):
    return isinstance(value, str) and value.isascii() and value.isdecimal() and str(int(value)) == value and 0 < int(value) < 2**64

class PublicationCheckJob:
    def __init__(self, cloud, bitbrowser, journal, checker_factory, *, cloud_env, capacity=None, records=None, admission_guard=None):
        self.cloud = cloud
        self.bitbrowser = bitbrowser
        self.profile_operations = journal.profile_operations
        self.publication_occupied = journal.profile_operations.occupied
        self.checker_factory = checker_factory
        self.records = records if records is not None else PublicationCheckStore(journal.database)
        self.cloud_env = cloud_env
        self.capacity = capacity or {'automatic_slots': 1, 'window_slots': 1}
        self._lock = Lock()
        self.admission_guard = admission_guard

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

    def _change(self, user, state, phase, **values):
        return self.records.transition(self.cloud_env, user, state['request_key'], state['phase'], phase, **values)

    def _safe_claim(self, claim, user):
        task, attempt, source = self._claim_identity(claim, user)
        if not _identity(claim.get('reservation_id')) or claim.get('reservation_state') != 'active':
            raise ValueError('original check reservation required')
        safe_source = {key: source[key] for key in ('profile_id', 'bit_profile_id', 'bit_main_user_id', 'platform_uid')}
        safe_source['content'] = {key: source['content'].get(key, '') for key in ('description', 'topics', 'declaration')}
        return dict(reservation_id=claim['reservation_id'], reservation_state='active', generation=claim['generation'],
            expected_revision=claim['expected_revision'], browser_fallback_allowed=claim.get('browser_fallback_allowed', False),
            task={key: task.get(key) for key in ('id', 'operator_id', 'status', 'platform', 'revision', 'current_attempt_id',
                'platform_account_uid', 'platform_submission_id', 'target_url')},
            attempt={**{key: attempt[key] for key in ('id', 'publication_id', 'executor_user_id', 'profile_id')}, 'input_snapshot': safe_source})

    @staticmethod
    def _ack_matches(ack, claim):
        return isinstance(ack, Mapping) and ack.get('released') is True and all(ack.get(key) == value for key, value in {
            'publication_id': claim['task']['id'], 'attempt_id': claim['attempt']['id'],
            'generation': claim['generation'], 'reservation_id': claim['reservation_id']}.items())

    @staticmethod
    def _release(claim, disposition):
        return dict(release_only=True, reservation_id=claim['reservation_id'], attempt_id=claim['attempt']['id'],
            generation=claim['generation'], profile_disposition=disposition)

    def _settle_unreceived(self, token, user, state):
        ack = self.cloud.settle_unreceived_publication_check(token, state['request_key'])
        cancelled = (isinstance(ack, Mapping) and ack.get('reservation_state') == 'cancelled'
                     and ack.get('request_key') == state['request_key'])
        released = (isinstance(ack, Mapping) and ack.get('reservation_state') == 'released' and ack.get('released') is True
                    and all(_identity(ack.get(key)) for key in ('publication_id', 'attempt_id', 'generation', 'reservation_id')))
        if not cancelled and not released:
            raise ValueError('unreceived check settlement has no terminal acknowledgement')
        self._change(user, state, 'settled')

    def _flush(self, token, user, state, current):
        claim, packet = state['claim'], state['packet']
        if not packet.get('release_only') and not current():
            packet = self._release(claim, packet['profile_disposition'])
            if not self._change(user, state, 'packet', packet=packet):
                return
        try:
            ack = (self.cloud.release_publication_check(token, claim['task']['id'], packet) if packet.get('release_only')
                   else self.cloud.observe_publication(token, claim['task']['id'], packet))
        except CloudError as error:
            if packet.get('release_only') or not (isinstance(error, StaleAttemptError) or error.code == 'publication_conflict'):
                raise
            release = self._release(claim, packet['profile_disposition'])
            self._change(user, state, 'packet', packet=release)
            return
        if not self._ack_matches(ack, claim):
            raise ValueError('original check settlement acknowledgement differs')
        self._change(user, state, 'settled')

    def tick(self, token, user, *, current):
        if not self._lock.acquire(blocking=False):
            return
        try:
            state = self.records.get(self.cloud_env, user)
            if state and state['phase'] != 'settled':
                if state['phase'] == 'claiming':
                    if not self._change(user, state, 'settling'):
                        return
                    state = self.records.get(self.cloud_env, user)
                if state['phase'] == 'settling':
                    return self._settle_unreceived(token, user, state)
                if state['phase'] == 'packet':
                    return self._flush(token, user, state, current)
                return self._read(token, user, state, current, recovering=True)
            if not current():
                return
            failure = None
            accepted = False
            settling = False
            # Only admission and durable acceptance share the machine-wide window barrier.
            # Existing recovery above must never wait for capacity to become available.
            guard = self.admission_guard() if self.admission_guard is not None else nullcontext(True)
            with guard as admitted:
                if not admitted or not current():
                    return
                state = self.records.begin(self.cloud_env, user, self.capacity)
                if state['phase'] != 'claiming':
                    return
                try:
                    claim = self.cloud.claim_publication_check(token, state['request_key'], capacity=state['capacity'])
                except Exception as error:
                    failure = error
                    settling = self._change(user, state, 'settling')
                else:
                    if claim is None:
                        self._change(user, state, 'settled')
                        return
                    if claim.get('reservation_state') in {'released', 'cancelled'}:
                        settling = self._change(user, state, 'settling')
                    else:
                        safe = self._safe_claim(claim, user)
                        accepted = self._change(user, state, 'active', claim=safe)
            if settling:
                self._settle_unreceived(token, user, self.records.get(self.cloud_env, user))
            if failure is not None:
                raise failure
            if accepted:
                return self._read(token, user, self.records.get(self.cloud_env, user), current, recovering=False)
        finally:
            self._lock.release()

    def _read(self, token, user, state, current, *, recovering):
        claim = state['claim']
        task, attempt, source = self._claim_identity(claim, user)
        profile_id = source['bit_profile_id']
        def before_open():
            if not current() or not self._change(user, state, 'possible_open'):
                raise PublicationCheckError('original check cannot open')
            state['phase'] = 'possible_open'
        def opened(endpoint):
            BrowserSession(endpoint)  # Validate a local, credential-free endpoint before persistence.
            if not self._change(user, state, 'possible_open', endpoint=endpoint):
                raise PublicationCheckError('original check endpoint could not be recorded')
            state['endpoint'] = endpoint
        resources = ProfileReadSession(self.bitbrowser, profile_id, before_open=before_open, opened=opened)
        observation = PublicWorkObservation('uncertain', '暂未完成指定稿件核验', '本轮未上传或投稿，保留原结果')
        disposition = 'not_opened'
        try:
            with self.profile_operations.registration(profile_id):
                if self.publication_occupied(profile_id):
                    return
                actual = self.records.get(self.cloud_env, user)
                if actual != state:
                    return
                profiles = self.bitbrowser.scan_profiles().profiles
                if len([p for p in profiles if p.bit_profile_id == profile_id and p.main_user_id == source['bit_main_user_id']]) != 1:
                    return
                if state['phase'] == 'possible_open':
                    # Never open again. A missing original endpoint cannot prove socket closure.
                    if not state.get('endpoint') or self.bitbrowser.close_profile_verified(profile_id, state['endpoint']) is not True:
                        return
                    resources.closed = True
                    resources.browser_used = True
                    disposition = 'closed'
                elif not current():
                    if self.bitbrowser.is_profile_closed(profile_id) is not True:
                        return
                    resources.closed = True
                else:
                    reference = PublicationReference(source['platform_uid'], task['platform_submission_id'], dict(source['content']), task.get('target_url') or '')
                    with resources:
                        observation = self.checker_factory(task['platform'])().check(reference, PublicationCheckControl(
                            current=current, browser_fallback_allowed=claim['browser_fallback_allowed'],
                            read_environment=resources.read_environment, browser=resources.browser))
                    if resources.browser_used:
                        disposition = 'closed'
                if not resources.closed:
                    return
                packet = self._release(claim, disposition)
                if current() and not (recovering and state['phase'] == 'possible_open'):
                    packet = self._observation(claim, source, observation, disposition, resources.browser_used)
                if self._change(user, state, 'packet', packet=packet):
                    state.update(phase='packet', packet=packet)
                    self._flush(token, user, state, current)
        except Exception as error:
            # Retain the original reservation on any unproven close/read/transport failure.
            location = error.__traceback__
            failure_path = []
            while location:
                code = location.tb_frame.f_code
                if 'wt_media_agent' in Path(code.co_filename).parts:
                    failure_path.append(f'{Path(code.co_filename).name}:{code.co_name}:{location.tb_lineno}')
                if location.tb_next is None:
                    break
                location = location.tb_next
            logger.info('publication check deferred error_type=%s failure_site=%s:%s:%s failure_path=%s',
                type(error).__name__, Path(location.tb_frame.f_code.co_filename).name,
                location.tb_frame.f_code.co_name, location.tb_lineno, '>'.join(failure_path))

    @staticmethod
    def _observation(claim, source, observation, disposition, browser_used):
        if not isinstance(observation, PublicWorkObservation) or observation.conclusion not in {'uncertain', 'reviewing', 'published'}:
            raise PublicationCheckError('unsupported public observation')
        public = observation.conclusion == 'published'
        value = {'attempt_id': claim['attempt']['id'], 'expected_revision': claim['expected_revision'],
            'conclusion': observation.conclusion, 'evidence': {
                'platform_uid': source['platform_uid'], 'reason': observation.reason, 'basis': observation.basis,
                'scene_closed': True, 'work_matched': observation.conclusion in {'reviewing', 'published'},
                'publicly_accessible': public, 'no_publishable_work': False}}
        if observation.conclusion in {'reviewing', 'published'}:
            value.update(platform_submission_id=observation.submission_id, target_url=observation.target_url)
        if public:
            if observation.published_at is None:
                raise PublicationCheckError('public check has no platform publication time')
            value.update(platform_work_id=observation.work_id, work_url=observation.work_url,
                published_at=observation.published_at.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'))
        packet = dict(reservation_id=claim['reservation_id'], generation=claim['generation'], observation=value, profile_disposition=disposition)
        if observation.lookup_result:
            packet['lookup_result'] = observation.lookup_result
        if browser_used:
            packet['browser_used'] = True
        return packet
