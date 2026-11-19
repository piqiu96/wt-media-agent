"""Machine-wide download directory, stored through the shared ORM resources."""
from sqlalchemy.dialects.sqlite import insert
from wt_media_agent.runtime.constants import SAVE_DIRECTORY_KEY
from wt_media_agent.storage.database import Database
from wt_media_agent.storage.models import AgentMetadata
from wt_media_agent.utils.time import utc_now_iso

class SaveDirectoryStore:
    def __init__(self, database: Database):
        self.database = database
        self._db_path = database.path

    def get(self):
        with self.database.session() as session:
            row=session.get(AgentMetadata,SAVE_DIRECTORY_KEY)
            return (row.value or None) if row else None

    def set(self, directory: str):
        statement=insert(AgentMetadata).values(key=SAVE_DIRECTORY_KEY,value=directory,updated_at=utc_now_iso())
        with self.database.session.begin() as session:
            session.execute(statement.on_conflict_do_update(index_elements=['key'],set_={'value':statement.excluded.value,'updated_at':statement.excluded.updated_at}))
