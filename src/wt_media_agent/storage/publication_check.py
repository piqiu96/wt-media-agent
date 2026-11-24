"""Owner/environment scoped recovery journal, never a Cloud business model."""
import json
import uuid
from sqlalchemy import select, update
from wt_media_agent.storage.database import retry_write
from wt_media_agent.storage.models import AgentMetadata
from wt_media_agent.storage.publication import PublicationClaimStore
from wt_media_agent.utils.time import utc_now_iso


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class PublicationCheckStore:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def _key(environment, user):
        return PublicationClaimStore._key(environment, user).replace('publication.claim.', 'publication.check.', 1)

    def get(self, environment, user):
        with self.database.session() as session:
            row = session.get(AgentMetadata, self._key(environment, user))
            return json.loads(row.value) if row else None

    @retry_write
    def begin(self, environment, user, capacity):
        key = self._key(environment, user)
        value = _json(dict(request_key=str(uuid.uuid4()), phase='claiming', capacity=capacity))
        with self.database.session.begin() as session:
            self.database.insert_if_absent(session, AgentMetadata, dict(key=key, value=value, updated_at=utc_now_iso()))
            row = session.get(AgentMetadata, key)
            state = json.loads(row.value)
            if state['phase'] == 'settled':
                changed = session.execute(update(AgentMetadata).where(AgentMetadata.key == key, AgentMetadata.value == row.value)
                    .values(value=value, updated_at=utc_now_iso()))
                session.expire_all()
                if changed.rowcount:
                    return json.loads(value)
                return json.loads(session.get(AgentMetadata, key).value)
            return state

    @retry_write
    def transition(self, environment, user, request_key, phase, next_phase, **changes):
        key = self._key(environment, user)
        with self.database.session.begin() as session:
            row = session.get(AgentMetadata, key)
            if row is None:
                return False
            state = json.loads(row.value)
            if state['request_key'] != request_key or state['phase'] != phase:
                return False
            state.update(changes, phase=next_phase)
            changed = session.execute(update(AgentMetadata).where(AgentMetadata.key == key, AgentMetadata.value == row.value)
                .values(value=_json(state), updated_at=utc_now_iso()))
            return changed.rowcount == 1

    def profile_occupied(self, profile_id):
        # Cross-environment local exclusion: a real window belongs to this machine.
        with self.database.session() as session:
            values = session.scalars(select(AgentMetadata.value).where(AgentMetadata.key.startswith('publication.check.')))
            for raw in values:
                state = json.loads(raw)
                if state['phase'] != 'settled' and state.get('claim', {}).get('attempt', {}).get('input_snapshot', {}).get('bit_profile_id') == profile_id:
                    return True
        return False

    def retained_window_count(self):
        """Potentially usable local reservations, across every owner/environment."""
        with self.database.session() as session:
            values = session.scalars(select(AgentMetadata.value).where(AgentMetadata.key.startswith('publication.check.')))
            count = 0
            for raw in values:
                state = json.loads(raw)
                if not state.get('claim') or state['phase'] == 'settled':
                    continue
                packet = state.get('packet', {})
                closed = (state['phase'] == 'packet' and packet.get('profile_disposition') in {'not_opened', 'closed'}
                          and (packet.get('release_only') is True or packet.get('observation', {}).get('evidence', {}).get('scene_closed') is True))
                if not closed:
                    count += 1
            return count
