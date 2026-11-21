"""A fresh owner-bound publication, with durable barriers and original-packet recovery."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from threading import Event, Thread, RLock

from wt_media_agent.runtime.constants import (
    DEFAULT_PUBLICATION_HEARTBEAT_SECONDS,
    PUBLICATION_RESOLVED_STATUSES,
    PUBLICATION_INPUT_CHANGED,
    PUBLICATION_NOT_SUBMITTED,
    PUBLICATION_RESULT_UNCERTAIN,
    PUBLICATION_RESULT_UNCERTAIN_MESSAGE,
    PublicationPhase,
    PublicationReport,
    PublicationStage,
    PublicationStatus,
    PublishMethod,
)
from wt_media_agent.runner.publication_preparation import CloudExecutionStopped
from wt_media_agent.storage.publication import JournalConflict
from wt_media_agent.platforms.publish import PublishControl, PublishPayload

logger = logging.getLogger(__name__)


class PublicationJob:
    def __init__(self, preparation, bitbrowser, platform_factory, *, heartbeat_interval=DEFAULT_PUBLICATION_HEARTBEAT_SECONDS):
        self.preparation = preparation
        self.cloud = preparation.cloud
        self.journal = preparation.journal
        self.bitbrowser = bitbrowser
        self.platform_factory = platform_factory
        self.heartbeat_interval = heartbeat_interval
        self._human_lock = RLock()
        self._human = None
        self._human_confirmation = None

    def handle_human_action(self,token,user_id,task_id,attempt_id,revision,*,resume):
        key = (token,user_id,task_id,attempt_id,revision)
        with self._human_lock:
            confirmation = self._human_confirmation
            if resume and confirmation is not None and confirmation['key'] == key and confirmation['current']():
                return dict(confirmation['result']) # An acknowledgement, never another execution event.
            scene = self._human
            if (scene is None or scene['token'] != token or not scene['current']()
                    or scene['resume'].is_set()
                    or (scene['identity'].user_id,scene['identity'].task_id,scene['identity'].attempt_id) != (user_id,task_id,attempt_id)
                    or self.journal.get(scene['identity'])['phase'] not in {PublicationPhase.CLAIMED,PublicationPhase.PREPARED}):
                raise JournalConflict('original live human scene is unavailable')
            feedback = self.cloud.publication_human(token,task_id,attempt_id,revision,resume=resume)
            expected = PublicationStatus.RUNNING if resume else PublicationStatus.AWAITING_HUMAN
            if (feedback.get('attempt_id') != attempt_id or feedback.get('stop_requested') is not False
                    or feedback.get('allow_submit') is not False or feedback.get('task_status') != expected
                    or not scene['current']()):
                raise JournalConflict('Cloud did not confirm the original scene')
            result={'attempt_id':attempt_id,'status':expected}
            if resume:
                self._human_confirmation={'key':key,'current':scene['current'],'result':result}
            scene['resume' if resume else 'show'].set()
            return dict(result)

    def run(self, token, claim, identity, *, current=lambda: True):
        if self.journal.get(identity)['phase'] != PublicationPhase.CLAIMED:
            raise JournalConflict('recovered attempts cannot enter the browser')
        done, stopped = Event(), Event()
        prepared = None
        stop_reason = ""
        receipt = {}
        submission_denied = False
        source = claim['attempt']['input_snapshot']
        evidence = {key: source[key] for key in ('platform_uid', 'video_sha256', 'cover_sha256')}
        evidence['bit_profile_id'] = identity.bit_profile_id

        def progress(stage, seq):
            nonlocal stop_reason
            feedback = self.cloud.report_publication(token, identity.task_id, PublicationReport.REPORT,
                dict(evidence, attempt_id=identity.attempt_id, report_seq=seq, stage=stage))
            if feedback.get('stop_reason') == PUBLICATION_INPUT_CHANGED:
                stop_reason = PUBLICATION_INPUT_CHANGED
            return feedback

        def should_stop():
            return stopped.is_set() or not current()

        def on_prepared():
            nonlocal stop_reason
            feedback = self.preparation.mark_ready(token, identity)
            if feedback.get('stop_reason') == PUBLICATION_INPUT_CHANGED:
                stop_reason = PUBLICATION_INPUT_CHANGED
            if feedback.get('stop_requested') is not False:
                raise JournalConflict('Cloud stopped the upload')

        def wait_for_human(show):
            if should_stop() or self.journal.get(identity)['phase'] not in {PublicationPhase.CLAIMED,PublicationPhase.PREPARED}:
                raise JournalConflict('human continuation is unavailable after submission')
            with self.preparation.report_lock:
                feedback = progress(PublicationStage.AWAITING_HUMAN,self.journal.reserve_report(identity))
            if feedback.get('stop_requested') is not False:
                raise JournalConflict('Cloud stopped the human scene')
            scene = {'token':token,'identity':identity,'current':lambda:not should_stop(),
                     'show':Event(),'resume':Event()}
            with self._human_lock:
                if self._human is not None:
                    raise JournalConflict('a human scene is already active')
                self._human_confirmation = None
                self._human = scene
            try:
                while not scene['resume'].wait(.1):
                    if should_stop():
                        raise JournalConflict('original human scene stopped')
                    if scene['show'].is_set():
                        scene['show'].clear()
                        show() # Runs only on the original browser thread.
                if should_stop():
                    raise JournalConflict('original human scene stopped')
            finally:
                with self._human_lock:
                    if self._human is scene:
                        self._human = None

        def before_submit():
            nonlocal submission_denied
            with self.preparation.report_lock:
                seq = self.journal.advance(identity, PublicationPhase.SUBMITTING)
                feedback = progress(PublicationStage.SUBMITTING, seq)
                submission_denied = feedback.get('allow_submit') is False and feedback.get('stop_requested') is True
                if feedback.get('allow_submit') is not True or feedback.get('stop_requested') is not False:
                    raise JournalConflict('no fresh submit permission')
                self.journal.advance(identity, PublicationPhase.CLICK_STARTED)

        def heartbeat():
            while not done.wait(self.heartbeat_interval):
                if not current():
                    stopped.set()
                    return
                try:
                    with self.preparation.report_lock:
                        seq = self.journal.reserve_report(identity, keepalive=True)
                        feedback = self.cloud.report_publication(token, identity.task_id, PublicationReport.REPORT,
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
                feedback = progress(PublicationStage.PREPARING, self.journal.reserve_report(identity))
            if feedback.get('stop_requested') is not False:
                raise JournalConflict('Cloud stopped the preparation')
            pulse = Thread(target=heartbeat, name='publication-heartbeat', daemon=True)
            pulse.start()
            # Reject unsupported platforms before resource preparation opens a Profile.
            publisher_factory = self.platform_factory(claim['task']['platform'])
            prepared = self.preparation.prepare(token, claim, identity)
            self.preparation.consume_prepared(prepared)
            if should_stop():
                raise JournalConflict('execution is no longer current')
            publisher = publisher_factory(prepared.endpoint)
            replace_draft=self.journal.may_replace_previous_draft(identity)
            if not replace_draft:
                previous=self.journal.previous_draft_attempt(identity)
                if previous is not None:
                    try:
                        original=self.cloud.original_publication_attempt(token,identity.task_id,previous.attempt_id)
                        self.journal.settle_resolved(previous,original)
                        replace_draft=self.journal.may_replace_previous_draft(identity)
                    except Exception:
                        pass  # Missing verification never grants draft replacement.
            receipt = publisher.publish(
                PublishPayload(prepared.video, prepared.cover, source['content'], source['platform_uid']),
                PublishControl(stopped=should_stop, on_prepared=on_prepared, before_submit=before_submit,
                    replace_matching_draft=replace_draft, wait_for_human=wait_for_human),
                method=PublishMethod.BROWSER)
            work_id = receipt.get('platform_work_id', '')
            if not isinstance(work_id, str) or not work_id.isascii() or not work_id.isdecimal() or len(work_id) > 255 or int(work_id) <= 0 or str(int(work_id)) != work_id:
                raise JournalConflict('no credible platform receipt')
        except Exception as error:
            if isinstance(error, CloudExecutionStopped) and error.reason == PUBLICATION_INPUT_CHANGED:
                stop_reason = PUBLICATION_INPUT_CHANGED
            location = error.__traceback__
            failure_path=[]
            while location is not None:
                frame_code=location.tb_frame.f_code
                if 'wt_media_agent' in Path(frame_code.co_filename).parts:
                    failure_path.append(f'{Path(frame_code.co_filename).name}:{frame_code.co_name}:{location.tb_lineno}')
                if location.tb_next is None:
                    break
                location=location.tb_next
            code = location.tb_frame.f_code
            network_error = re.search(r"net::ERR_[A-Z_]+\b", str(error))
            logger.warning("publication stopped before a credible result: phase=%s error_type=%s failure_site=%s:%s:%s browser_error=%s failure_path=%s",
                self.journal.get(identity)['phase'], type(error).__name__,
                Path(code.co_filename).name, code.co_name, location.tb_lineno,
                network_error.group(0) if network_error else "unknown", ">".join(failure_path))
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
        result = {'status': PublicationStatus.UNCERTAIN, 'error_code': PUBLICATION_RESULT_UNCERTAIN,
                  'error_message': PUBLICATION_RESULT_UNCERTAIN_MESSAGE, 'evidence': evidence}
        if not receipt and closed is True and (self.journal.get(identity)['phase'] in {PublicationPhase.CLAIMED, PublicationPhase.PREPARED} or submission_denied):
            evidence['not_submitted'] = True
            result = {'status': PublicationStatus.FAILED,
                      'error_code': stop_reason or PUBLICATION_NOT_SUBMITTED,
                      'error_message': 'Execution stopped before submission; the browser scene was closed.',
                      'evidence': evidence}
        if receipt:
            evidence['receipt_id'] = receipt['platform_work_id']
            result['platform_work_id'] = receipt['platform_work_id']
            if closed is True:
                result = {'status': PublicationStatus.REVIEWING, 'platform_work_id': receipt['platform_work_id'], 'evidence': evidence}
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
        pending=self.journal.pending_attempts(self.preparation.cloud_env,user_id)
        attempts={row['identity'].key:row for row in self.journal.unresolved_attempts(self.preparation.cloud_env,user_id)}
        attempts.update({row['identity'].key:row for row in pending})
        for row in attempts.values():
            identity, packet = row['identity'], row['packet']
            was_settled=self.journal.get(identity)['phase']==PublicationPhase.SETTLED
            expected = (identity.attempt_id, packet['result_hash']) if packet is not None else None
            acknowledgement = None
            resolved_status = ''
            scene_resolution = None
            try:
                if packet is not None:
                    wire = {key: value for key, value in packet.items() if key != 'task_id'}
                    with self.preparation.report_lock:
                        feedback = self.cloud.report_publication(token, identity.task_id, PublicationReport.COMPLETE, wire)
                    if (feedback.get('attempt_id'), feedback.get('result_hash')) == expected:
                        acknowledgement = feedback
                        if packet['result'].get('evidence', {}).get('scene_closed') is True and feedback.get('task_status') in PUBLICATION_RESOLVED_STATUSES:
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
                        if original.get('scene_resolved') is True and original.get('resolved_status') in PUBLICATION_RESOLVED_STATUSES:
                            resolved_status = original['resolved_status']
                except Exception:
                    pass
            try:
                if acknowledgement is not None:
                    self.journal.record_delivery(identity.cloud_env, user_id, packet, accepted=True)
                if resolved_status:
                    self.journal.settle(identity, packet['result_hash'], task_status=resolved_status)
                elif acknowledgement is None and scene_resolution is not None and not was_settled:
                    self.journal.settle_resolved(identity, scene_resolution)
                elif acknowledgement is None:
                    continue
                confirmed += 1
            except Exception:
                continue
        return confirmed
