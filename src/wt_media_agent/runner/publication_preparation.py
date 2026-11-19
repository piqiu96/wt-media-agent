"""Owner-bound preparation. This module cannot upload or submit a platform post.

The future platform executor must call the durable submission boundary; a
replayed claim or process restart is reconciliation work, never browser work.
"""
from __future__ import annotations
from dataclasses import dataclass
from wt_media_agent.runner.publication import PublicationDelivery
from pathlib import Path
from threading import RLock
from wt_media_agent.runtime.constants import (
    PublicationPhase,
    PublicationReport,
    PublicationStage,
    PublicationStatus,
)
from wt_media_agent.storage.publication import ExecutionIdentity, JournalConflict, _environment
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




class PublicationPreparation:
    def __init__(self, cloud, bitbrowser, journal, claims, directory, *, cloud_env: str):
        self.cloud_env=_environment(cloud_env)
        self.cloud=cloud;self.bitbrowser=bitbrowser;self.journal=journal;self.claims=claims;self.directory=Path(directory);self._fresh=set();self._prepared=set();self.report_lock=RLock()

    def claim(self, token: str, user_id: str):
        key=self.claims.pending(self.cloud_env,user_id)
        value=self.cloud.claim_publication(token,key)
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
            snapshot={k:source[k] for k in ('platform_uid','video_sha256','video_size','cover_sha256','cover_size')}
            snapshot['platform']=task['platform'];snapshot['content']={k:source['content'][k] for k in ('description','topics','declaration')}
            fresh=self.journal.register(identity,snapshot)
        except (KeyError,TypeError,ValueError):
            raise PublicationInputError('invalid owner-bound claim input') from None
        self.claims.acknowledge(self.cloud_env,user_id,key)
        if not fresh or value['replayed'] or task['status']!=PublicationStatus.RUNNING or attempt['status']!=PublicationStatus.RUNNING or attempt['stage']!=PublicationStage.CLAIMED:
            raise JournalConflict('original attempt requires verification; browser re-entry forbidden')
        self._fresh.add(identity.key)
        return value,identity

    def prepare(self, token: str, claim, identity: ExecutionIdentity) -> PreparedPublication:
        if identity.key not in self._fresh:
            raise JournalConflict('preparation requires a fresh claim in this process')
        self._fresh.remove(identity.key)
        state=self.journal.get(identity)
        if state['phase']!=PublicationPhase.CLAIMED:
            raise JournalConflict('preparation cannot be replayed')
        task=claim['task'];i=claim['attempt']['input_snapshot']
        profile=next((p for p in self.bitbrowser.scan_profiles().profiles if p.bit_profile_id==identity.bit_profile_id),None)
        if profile is None or profile.main_user_id!=i['bit_main_user_id']:
            raise PublicationInputError('BitBrowser owner or Profile differs')
        grants=claim.get('files',{})
        for name in ('video','cover'):
            g=grants.get(name,{})
            if g.get('sha256')!=i[name+'_sha256'] or g.get('size')!=i[name+'_size']:
                raise PublicationInputError('download grant differs from frozen input')
        folder=self.directory/identity.key.replace(':','-')
        video=folder/'video.mp4';cover=folder/'cover'
        download_verified(grants['video'],video);download_verified(grants['cover'],cover)
        endpoint=self.bitbrowser.open_profile_with_devtools(identity.bit_profile_id)
        self._prepared.add(identity.key)
        return PreparedPublication(identity,video,cover,endpoint)

    def mark_ready(self, token: str, identity: ExecutionIdentity):
        """The platform calls back only after initial live identity verification."""
        source=self.journal.get(identity)['input']
        # Persist before reporting progress. Losing this response never reopens
        # the browser; this attempt then requires explicit reconciliation.
        with self.report_lock:
            seq=self.journal.advance(identity,PublicationPhase.PREPARED)
            feedback=self.cloud.report_publication(token,identity.task_id,PublicationReport.REPORT,{
                'attempt_id':identity.attempt_id,'report_seq':seq,'stage':PublicationStage.READY,
                'platform_uid':source['platform_uid'],'bit_profile_id':identity.bit_profile_id,
                'video_sha256':source['video_sha256'],'cover_sha256':source['cover_sha256']})
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
