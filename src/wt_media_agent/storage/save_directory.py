"""Machine-wide download directory, stored through the shared ORM resources."""
from wt_media_agent.runtime.constants import SAVE_DIRECTORY_KEY
from wt_media_agent.storage.database import Database
from wt_media_agent.storage.models import AgentMetadata
from wt_media_agent.utils.time import utc_now_iso

class SaveDirectoryStore:
    def __init__(self, database: Database):
        self.database = database

    def get(self):
        with self.database.session() as session:
            row=session.get(AgentMetadata,SAVE_DIRECTORY_KEY)
            return (row.value or None) if row else None

    def set(self, directory: str):
        values = {'key': SAVE_DIRECTORY_KEY, 'value': directory, 'updated_at': utc_now_iso()}
        with self.database.session.begin() as session:
            self.database.upsert(session, AgentMetadata, values, {'value': directory, 'updated_at': values['updated_at']})
