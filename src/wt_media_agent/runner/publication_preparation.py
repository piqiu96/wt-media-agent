"""Owner-bound preparation. This module cannot upload or submit a platform post.

The future platform executor must call the durable submission boundary; a
replayed claim or process restart is reconciliation work, never browser work.
"""
from __future__ import annotations
from dataclasses import dataclass
import math
import time
from wt_media_agent.runner.publication import PublicationDelivery
from pathlib import Path
from threading import RLock
from contextlib import contextmanager
from wt_media_agent.runtime.constants import (
    PublicationPhase,
    PublicationReport,
    PublicationStage,
    PublicationStatus,
)
from wt_media_agent.storage.publication import ExecutionIdentity, JournalConflict, _environment, publication_content, publication_file_evidence
from wt_media_agent.platforms.publish import PublicationInputError
from wt_media_agent.services.files.verified_download import download_verified


class CloudExecutionStopped(PublicationInputError):
    def __init__(self, reason):
        super().__init__('Cloud stopped this execution')
        self.reason = reason


@dataclass(frozen=True)
class PreparedPublication:
    identity: ExecutionIdentity
    video: Path
    cover: Path
    endpoint: str
    portrait_cover: Path | None = None
    space_cover: Path | None = None




class PublicationPreparation:
    def __init__(self, cloud, bitbrowser, journal, claims, directory, *, cloud_env: str, capacity=None, preparation_timeout=3600.0):
        self.cloud_env=_environment(cloud_env)
        self.cloud=cloud;self.bitbrowser=bitbrowser;self.journal=journal;self.claims=claims;self.directory=Path(directory);self._fresh=set();self._prepared=set();self._report_locks={};self._locks_guard=RLock()
        if type(preparation_timeout) not in (int, float) or not math.isfinite(preparation_timeout) or preparation_timeout <= 0:
            raise ValueError('positive preparation timeout required')
        self.preparation_timeout=preparation_timeout
        self.capacity=capacity or {'automatic_slots':5,'window_slots':10}

    @contextmanager
    def reporting(self, identity):
        with self._locks_guard:
            lock = self._report_locks.setdefault(identity.key, RLock())
        with lock:
            yield

    def claim(self, token: str, user_id: str, *, capacity=None):
        key=self.claims.pending(self.cloud_env,user_id)
        value=self.cloud.claim_publication(token,key,capacity=capacity or self.capacity)
        if value is None:
            self.claims.acknowledge(self.cloud_env,user_id,key)
            return None
        # No browser access or request-key rotation until the response has been
        # checked and the original input is durable.
        try:
            task=value['task'];attempt=value['attempt'];source=attempt['input_snapshot']
            if task['operator_id']!=user_id or attempt['executor_user_id']!=user_id or attempt['publication_id']!=task['id'] or task['current_attempt_id']!=attempt['id'] or type(value['replayed']) is not bool:
                raise ValueError('owner/attempt differs')
            identity=ExecutionIdentity(self.cloud_env,user_id,task['id'],attempt['id'],source['bit_profile_id'])
            platform = task['platform']
            if platform not in ('baidu', 'bilibili') or source.get('platform', 'baidu') != platform:
                raise ValueError('frozen platform differs')
            secondary = 'space_cover' if platform == 'bilibili' else 'portrait_cover'
            if platform == 'bilibili' and (source.get('portrait_cover_sha256', '') != '' or source.get('portrait_cover_size', 0) != 0):
                raise ValueError('foreign secondary cover in Bilibili input')
            snapshot={k:source[k] for k in ('platform_uid','video_sha256','video_size','cover_sha256','cover_size',secondary+'_sha256',secondary+'_size')}
            snapshot['platform']=platform;snapshot['content']=publication_content(platform,source['content'])
            fresh=self.journal.register(identity,snapshot)
        except (KeyError,TypeError,ValueError):
            raise PublicationInputError('invalid owner-bound claim input') from None
        self.claims.acknowledge(self.cloud_env,user_id,key)
        if not fresh or value['replayed'] or task['status']!=PublicationStatus.RUNNING or attempt['status']!=PublicationStatus.RUNNING or attempt['stage']!=PublicationStage.CLAIMED:
            raise JournalConflict('original attempt requires verification; browser re-entry forbidden')
        self._fresh.add(identity.key)
        return value,identity

    def prepare(self, token: str, claim, identity: ExecutionIdentity, *, current=lambda: True) -> PreparedPublication:
        if identity.key not in self._fresh:
            raise JournalConflict('preparation requires a fresh claim in this process')
        self._fresh.remove(identity.key)
        state=self.journal.get(identity)
        if state['phase']!=PublicationPhase.CLAIMED:
            raise JournalConflict('preparation cannot be replayed')
        deadline=time.monotonic()+self.preparation_timeout
        def check_current():
            if not current() or time.monotonic() >= deadline:
                raise CloudExecutionStopped('credentials_changed')
        check_current()
        task=claim['task'];i=claim['attempt']['input_snapshot']
        profile=next((p for p in self.bitbrowser.scan_profiles().profiles if p.bit_profile_id==identity.bit_profile_id),None)
        if profile is None or profile.main_user_id!=i['bit_main_user_id']:
            raise PublicationInputError('BitBrowser owner or Profile differs')
        grants=claim.get('files',{})
        secondary = 'space_cover' if task['platform'] == 'bilibili' else 'portrait_cover'
        for name in ('video','cover',secondary):
            g=grants.get(name,{})
            if g.get('sha256')!=i[name+'_sha256'] or g.get('size')!=i[name+'_size']:
                raise PublicationInputError('download grant differs from frozen input')
        folder=self.directory/identity.key.replace(':','-')
        video=folder/'video.mp4';cover=folder/'cover'
        secondary_cover=folder/secondary.replace('_','-')
        # All grants are minted together. Open small resources first so a slow
        # video cannot consume their validity before their requests even start.
        resources=(('video',video),('cover',cover),(secondary,secondary_cover))
        for name, target in sorted(resources,key=lambda item:grants[item[0]]['size']):
            check_current()
            download_verified(grants[name],target,current=current,deadline=deadline)
        check_current()
        with self.journal.profile_operations.registration(identity.bit_profile_id):
            if not self.journal.profile_available(identity):
                raise JournalConflict('Profile has another unresolved local holder')
            check_current()
            endpoint=self.bitbrowser.open_profile_with_devtools(identity.bit_profile_id)
        self._prepared.add(identity.key)
        return PreparedPublication(identity,video,cover,endpoint,
            portrait_cover=secondary_cover if secondary=='portrait_cover' else None,
            space_cover=secondary_cover if secondary=='space_cover' else None)

    def mark_ready(self, token: str, identity: ExecutionIdentity):
        """The platform calls back only after initial live identity verification."""
        source=self.journal.get(identity)['input']
        # Persist before reporting progress. Losing this response never reopens
        # the browser; this attempt then requires explicit reconciliation.
        with self.reporting(identity):
            seq=self.journal.advance(identity,PublicationPhase.PREPARED)
            feedback=self.cloud.report_publication(token,identity.task_id,PublicationReport.REPORT,{
                'attempt_id':identity.attempt_id,'report_seq':seq,'stage':PublicationStage.READY,
                'bit_profile_id':identity.bit_profile_id, **publication_file_evidence(source)})
        if feedback.get('stop_requested') is not False:
            raise CloudExecutionStopped(feedback.get('stop_reason'))
        return feedback

    def consume_prepared(self, prepared: PreparedPublication):
        # In-memory, one-use capability. A restarted process cannot turn an old
        # prepared record into a new browser action or another submission.
        if prepared.identity.key not in self._prepared:
            raise JournalConflict('only fresh preparation may enter the platform')
        self._prepared.remove(prepared.identity.key)

    def flush(self, token: str, user_id: str):
        def send(packet):
            task_id=packet.pop('task_id')
            return self.cloud.report_publication(token,task_id,PublicationReport.COMPLETE,packet)
        return PublicationDelivery(self.journal).flush(self.cloud_env,user_id,send)
