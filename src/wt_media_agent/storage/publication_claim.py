"""Persist the request key before HTTP; never advance past an unknown claim."""
import hashlib
import json
import uuid
from contextlib import closing
from wt_media_agent.storage.sqlite import connect
from wt_media_agent.storage.publication import _environment
from wt_media_agent.utils.time import utc_now_iso

class PublicationClaimStore:
    def __init__(self, db_path): self.db_path = db_path
    def _key(self, environment, user_id):
        if not isinstance(user_id,str) or not user_id.isascii() or not user_id.isdecimal() or str(int(user_id))!=user_id or int(user_id)<=0:
            raise ValueError('canonical owner identity required')
        return 'publication.claim.' + hashlib.sha256(json.dumps([_environment(environment),user_id],separators=(',',':')).encode()).hexdigest()
    def pending(self, environment, user_id):
        key=self._key(environment,user_id)
        with closing(connect(self.db_path)) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            with db:
                row=db.execute('SELECT value FROM agent_metadata WHERE key=?',(key,)).fetchone()
                if row: return row['value']
                value=str(uuid.uuid4())
                db.execute('INSERT INTO agent_metadata(key,value,updated_at) VALUES(?,?,?)',(key,value,utc_now_iso()))
                return value
    def acknowledge(self, environment, user_id, request_key):
        # Only no-task or an already durable local attempt can acknowledge a claim.
        with closing(connect(self.db_path)) as db:
            db.execute('PRAGMA synchronous=FULL')
            with db: db.execute('DELETE FROM agent_metadata WHERE key=? AND value=?',(self._key(environment,user_id),request_key))
