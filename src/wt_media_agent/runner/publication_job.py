"""A fresh owner-bound publication, with durable barriers and original-packet recovery."""
from __future__ import annotations

import logging
from threading import Event, Thread

from wt_media_agent.storage.publication import JournalConflict
from wt_media_agent.clients.publisher import PublishPayload
from wt_media_agent.clients.publication import read_baidu_identity

logger = logging.getLogger(__name__)


class PublicationJob:
    def __init__(self, preparation, bitbrowser, platform_factory, *, browser_session_factory, heartbeat_interval=15.0):
        self.preparation = preparation
        self.cloud = preparation.cloud
        self.journal = preparation.journal
        self.bitbrowser = bitbrowser
        self.platform_factory = platform_factory
        self.browser_session_factory = browser_session_factory
        self.heartbeat_interval = heartbeat_interval

    def run(self, token, claim, identity, *, current=lambda: True):
        if self.journal.get(identity)['phase'] != 'claimed':
            raise JournalConflict('recovered attempts cannot enter the browser')
        done, stopped = Event(), Event()
        prepared = None
        receipt = {}
        source = claim['attempt']['input_snapshot']
        evidence = {key: source[key] for key in ('platform_uid', 'video_sha256', 'cover_sha256')}
        evidence['bit_profile_id'] = identity.bit_profile_id

        def progress(stage, seq):
            return self.cloud.report_publication(token, identity.task_id, 'progress',
                dict(evidence, attempt_id=identity.attempt_id, report_seq=seq, stage=stage))

        def should_stop():
            return stopped.is_set() or not current()

        def heartbeat():
            while not done.wait(self.heartbeat_interval):
                if not current():
                    stopped.set()
                    return
                try:
                    with self.preparation.report_lock:
                        seq = self.journal.get(identity)['report_seq']
                        feedback = self.cloud.report_publication(token, identity.task_id, 'heartbeat',
                            {'attempt_id': identity.attempt_id, 'report_seq': seq})
                    if feedback.get('stop_requested') is not False:
                        stopped.set()
                except Exception:
                    stopped.set()  # A missing response never grants a new action.

        pulse = None
        try:
            if should_stop():
                raise JournalConflict('execution credentials changed')
            with self.preparation.report_lock:
                feedback = progress('preparing', self.journal.reserve_report(identity))
            if feedback.get('stop_requested') is not False:
                raise JournalConflict('Cloud stopped the preparation')
            pulse = Thread(target=heartbeat, name='publication-heartbeat', daemon=True)
            pulse.start()
            prepared = self.preparation.prepare(token, claim, identity)
            self.preparation.consume_prepared(prepared)
            if should_stop():
                raise JournalConflict('execution is no longer current')
            # Re-check the current session immediately before starting upload.
            with self.preparation.report_lock:
                feedback = progress('ready', self.journal.reserve_report(identity))
            if feedback.get('stop_requested') is not False:
                raise JournalConflict('Cloud stopped the upload')
            platform = self.platform_factory(claim['task']['platform'])
            with self.browser_session_factory(prepared.endpoint) as session:
                platform.fill_form(session.page, PublishPayload(prepared.video, prepared.cover, source['content']), stopped=should_stop)
                if should_stop():
                    raise JournalConflict('execution stopped during upload')
                # Upload may take minutes. Re-read the active browser account
                # before requesting permission for this frozen identity.
                observed = read_baidu_identity(prepared.endpoint)
                if observed.get('login_status') != 'normal' or observed.get('platform_account_id') != source['platform_uid']:
                    raise JournalConflict('platform account changed before submit')
                if should_stop():
                    raise JournalConflict('execution stopped during identity check')
                with self.preparation.report_lock:
                    seq = self.journal.advance(identity, 'submitting')
                    feedback = progress('submitting', seq)
                    if feedback.get('allow_submit') is not True or feedback.get('stop_requested') is not False:
                        raise JournalConflict('no fresh submit permission')
                    self.journal.advance(identity, 'click_started')
                # This call is deliberately outside any retry or transaction.
                if should_stop():
                    raise JournalConflict('execution credentials changed before submit')
                platform.submit()
                receipt = platform.read_receipt()
            work_id = receipt.get('platform_work_id', '')
            if not isinstance(work_id, str) or not work_id.isascii() or not work_id.isdecimal() or len(work_id) > 255 or int(work_id) <= 0 or str(int(work_id)) != work_id:
                raise JournalConflict('no credible platform receipt')
        except Exception as error:
            logger.warning("publication stopped before a credible result: phase=%s error_type=%s",
                self.journal.get(identity)['phase'], type(error).__name__)
            # Do not persist exception text: platform errors may contain private
            # addresses or credentials. Keep the exact local phase and evidence.
            receipt = {}
        finally:
            done.set()
            if pulse is not None:
                pulse.join(timeout=1.0)

        closed = False
        if prepared is not None:
            try:
                closed = self.bitbrowser.close_profile_verified(identity.bit_profile_id, prepared.endpoint)
            except Exception:
                pass
        evidence['scene_closed'] = closed is True
        result = {'status': 'uncertain', 'error_code': 'PUBLICATION_RESULT_UNCERTAIN',
                  'error_message': '投稿结果或执行现场尚未核实，请核对原尝试；不会自动重新投稿。', 'evidence': evidence}
        if receipt:
            evidence['receipt_id'] = receipt['platform_work_id']
            result['platform_work_id'] = receipt['platform_work_id']
            if closed is True:
                result = {'status': 'reviewing', 'platform_work_id': receipt['platform_work_id'], 'evidence': evidence}
            # A receipt is not a verified public link. Public URLs are attached
            # only by the later public-result verification flow.
        # Save first; a network failure below only leaves this exact packet to
        # replay. It cannot re-enter preparation or the platform.
        with self.preparation.report_lock:
            seq = self.journal.get(identity)['report_seq'] + 1
            self.journal.save_result(identity, result, report_seq=seq)
        self.recover(token, identity.user_id)
        return result

    def recover(self, token, user_id):
        confirmed = 0
        for row in self.journal.unresolved_attempts(self.cloud.base_url, user_id):
            identity, packet = row['identity'], row['packet']
            expected = (identity.attempt_id, packet['result_hash']) if packet is not None else None
            acknowledgement = None
            resolved_status = ''
            scene_resolution = None
            try:
                if packet is not None:
                    wire = {key: value for key, value in packet.items() if key != 'task_id'}
                    with self.preparation.report_lock:
                        feedback = self.cloud.report_publication(token, identity.task_id, 'complete', wire)
                    if (feedback.get('attempt_id'), feedback.get('result_hash')) == expected:
                        acknowledgement = feedback
                        if packet['result'].get('evidence', {}).get('scene_closed') is True and feedback.get('task_status') in ('reviewing', 'failed'):
                            resolved_status = feedback['task_status']
            except Exception:
                pass
            if not resolved_status:
                try:
                    # The task may already have advanced. Read this exact old
                    # attempt, not the current task's success or authorization.
                    original = self.cloud.original_publication_attempt(token, identity.task_id, identity.attempt_id)
                    scene_resolution = original
                    if (original.get('attempt_id'), original.get('result_hash')) == expected:
                        acknowledgement = original
                        if original.get('scene_resolved') is True and original.get('resolved_status') in ('reviewing', 'failed'):
                            resolved_status = original['resolved_status']
                except Exception:
                    pass
            try:
                if acknowledgement is not None:
                    self.journal.flush(identity.environment, user_id,
                        lambda pending: acknowledgement if pending == packet else None,
                        only_attempt=identity.attempt_id)
                if resolved_status:
                    self.journal.settle(identity, packet['result_hash'], task_status=resolved_status)
                elif acknowledgement is None and scene_resolution is not None:
                    self.journal.settle_resolved(identity, scene_resolution)
                elif acknowledgement is None:
                    continue
                confirmed += 1
            except Exception:
                continue
        return confirmed
