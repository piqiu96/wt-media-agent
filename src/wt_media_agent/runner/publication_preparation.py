"""Owner-bound preparation. This module cannot upload or submit a platform post.

The future platform executor must call the durable submission boundary; a
replayed claim or process restart is reconciliation work, never browser work.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from wt_media_agent.storage.publication import ExecutionIdentity, JournalConflict
from wt_media_agent.clients.publication import PublicationInputError,download_verified,live_baidu_identity


@dataclass(frozen=True)
class PreparedPublication:
    identity: ExecutionIdentity
    video: Path
    cover: Path
    endpoint: str




class PublicationPreparation:
    def __init__(self, cloud, bitbrowser, journal, claims, directory):
        self.cloud=cloud;self.bitbrowser=bitbrowser;self.journal=journal;self.claims=claims;self.directory=Path(directory);self._fresh=set();self._prepared=set();self.report_lock=RLock()

    def claim(self, token: str, user_id: str):
        key=self.claims.pending(self.cloud.base_url,user_id)
        value=self.cloud.claim_publication(token,key)
        if value is None:
            self.claims.acknowledge(self.cloud.base_url,user_id,key)
            return None
        # No browser access or request-key rotation until the response has been
        # checked and the original input is durable.
        try:
            task=value['task'];attempt=value['attempt'];source=attempt['input_snapshot']
            if task['operator_id']!=user_id or attempt['executor_user_id']!=user_id or attempt['publication_id']!=task['id'] or task['current_attempt_id']!=attempt['id'] or type(value['replayed']) is not bool:
                raise ValueError('owner/attempt differs')
            identity=ExecutionIdentity(self.cloud.base_url,user_id,task['id'],attempt['id'],source['bit_profile_id'])
            snapshot={k:source[k] for k in ('platform_uid','video_sha256','video_size','cover_sha256','cover_size')}
            snapshot['platform']=task['platform'];snapshot['content']={k:source['content'][k] for k in ('description','topics','declaration')}
            fresh=self.journal.register(identity,snapshot)
        except (KeyError,TypeError,ValueError):
            raise PublicationInputError('invalid owner-bound claim input') from None
        self.claims.acknowledge(self.cloud.base_url,user_id,key)
        if not fresh or value['replayed'] or task['status']!='running' or attempt['status']!='running' or attempt['stage']!='claimed':
            raise JournalConflict('original attempt requires verification; browser re-entry forbidden')
        self._fresh.add(identity.key)
        return value,identity

    def prepare(self, token: str, claim, identity: ExecutionIdentity) -> PreparedPublication:
        if identity.key not in self._fresh:
            raise JournalConflict('preparation requires a fresh claim in this process')
        self._fresh.remove(identity.key)
        state=self.journal.get(identity)
        if state['phase']!='claimed':
            raise JournalConflict('preparation cannot be replayed')
        task=claim['task'];i=claim['attempt']['input_snapshot']
        if task['platform']!='baidu': raise PublicationInputError('platform not implemented')
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
        endpoint,observed=live_baidu_identity(self.bitbrowser,identity.bit_profile_id)
        if observed.get('login_status')!='normal' or observed.get('platform_account_id')!=i['platform_uid']:
            raise PublicationInputError('live platform UID differs or login unavailable')
        # Persist before reporting progress. Losing this response never reopens
        # the browser; this attempt then requires explicit reconciliation.
        with self.report_lock:
            seq=self.journal.advance(identity,'prepared')
            feedback=self.cloud.report_publication(token,task['id'],'progress',{
                'attempt_id':identity.attempt_id,'report_seq':seq,'stage':'ready',
                'platform_uid':observed['platform_account_id'],'bit_profile_id':identity.bit_profile_id,
                'video_sha256':i['video_sha256'],'cover_sha256':i['cover_sha256']})
        if feedback.get('stop_requested') is not False:
            raise PublicationInputError('Cloud stopped this execution')
        self._prepared.add(identity.key)
        return PreparedPublication(identity,video,cover,endpoint)

    def consume_prepared(self, prepared: PreparedPublication):
        # In-memory, one-use capability. A restarted process cannot turn an old
        # prepared record into a new browser action or another submission.
        if prepared.identity.key not in self._prepared:
            raise JournalConflict('only fresh preparation may enter the platform')
        self._prepared.remove(prepared.identity.key)

    def flush(self, token: str, user_id: str):
        def send(packet):
            task_id=packet.pop('task_id')
            return self.cloud.report_publication(token,task_id,'complete',packet)
        return self.journal.flush(self.cloud.base_url,user_id,send)
