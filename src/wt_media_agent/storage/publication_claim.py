"""Persist a scoped claim request before HTTP; acknowledge only durable outcomes."""
import uuid
from sqlalchemy import delete
from sqlalchemy.dialects.sqlite import insert
from wt_media_agent.runtime.constants import CLOUD_ENVS
from wt_media_agent.storage.database import Database
from wt_media_agent.storage.models import AgentMetadata
from wt_media_agent.utils.time import utc_now_iso

class PublicationClaimStore:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _key(cloud_env: str, user_id: str) -> str:
        if cloud_env not in CLOUD_ENVS:
            raise ValueError('explicit Cloud environment required')
        if (not isinstance(user_id,str) or not user_id.isascii() or not user_id.isdecimal()
                or str(int(user_id))!=user_id or not 0<int(user_id)<2**64):
            raise ValueError('canonical owner identity required')
        return f'publication.claim.{cloud_env}.{user_id}'

    def pending(self, cloud_env: str, user_id: str) -> str:
        key=self._key(cloud_env,user_id)
        with self.database.session.begin() as session:
            session.execute(insert(AgentMetadata).values(key=key,value=str(uuid.uuid4()),updated_at=utc_now_iso()).on_conflict_do_nothing(index_elements=['key']))
            return session.get(AgentMetadata,key).value

    def acknowledge(self, cloud_env: str, user_id: str, request_key: str):
        with self.database.session.begin() as session:
            session.execute(delete(AgentMetadata).where(AgentMetadata.key==self._key(cloud_env,user_id),AgentMetadata.value==request_key))
