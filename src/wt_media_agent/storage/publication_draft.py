"""One revisioned input per environment/user/type, independent of execution."""
import json
import re
from sqlalchemy import select, update
from wt_media_agent.runtime.constants import CLOUD_ENVS
from wt_media_agent.storage.database import retry_write
from wt_media_agent.storage.models import PublicationDraftModel
from wt_media_agent.utils.time import utc_now_iso

MAX_DRAFT_BYTES = 2 * 1024 * 1024


class DraftConflict(Exception):
    pass


class PublicationDraftStore:
    def __init__(self, database, *, environment):
        if environment not in CLOUD_ENVS:
            raise ValueError('invalid draft environment')
        self.database = database
        self.environment = environment

    def _scope(self, user_id, draft_type):
        if not isinstance(user_id, str) or not re.fullmatch(r'[1-9][0-9]*', user_id):
            raise ValueError('verified user required')
        if draft_type != 'task':
            raise ValueError('unsupported draft type')
        return dict(environment=self.environment, user_id=user_id, draft_type=draft_type)

    @staticmethod
    def _view(row):
        if row is None:
            return dict(payload=None, revision='0', saved_at=None, schema_version=1)
        return dict(payload=json.loads(row.payload_json), revision=str(row.revision), saved_at=row.saved_at, schema_version=row.schema_version)

    def get(self, user_id, draft_type):
        scope = self._scope(user_id, draft_type)
        with self.database.session() as session:
            return self._view(session.scalar(select(PublicationDraftModel).filter_by(**scope)))

    def put(self, user_id, draft_type, payload, expected_revision):
        if not isinstance(payload, dict):
            raise ValueError('draft payload must be an object')
        return self._write(user_id, draft_type, payload, expected_revision)

    def clear(self, user_id, draft_type, expected_revision):
        return self._write(user_id, draft_type, None, expected_revision)

    @retry_write
    def _write(self, user_id, draft_type, payload, expected_revision):
        scope = self._scope(user_id, draft_type)
        if not isinstance(expected_revision, str) or not re.fullmatch(r'0|[1-9][0-9]*', expected_revision) or len(expected_revision) > 19:
            raise ValueError('expected revision must be a decimal string')
        expected = int(expected_revision)
        if expected >= 2**63 - 1:
            raise ValueError('revision out of range')
        try:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
            if len(encoded.encode('utf-8')) > MAX_DRAFT_BYTES:
                raise ValueError('draft exceeds size limit')
        except (TypeError, OverflowError, RecursionError) as error:
            raise ValueError('invalid draft payload') from error
        with self.database.session.begin() as session:
            row = session.scalar(select(PublicationDraftModel).filter_by(**scope))
            current = row.revision if row else 0
            # Only the immediate matching result is a replay; an older request
            # cannot rewrite a later save/clear, even if its payload matches.
            if current != expected:
                if row and current == expected + 1 and row.payload_json == encoded:
                    return self._view(row)
                raise DraftConflict('draft changed; read before saving')
            values = dict(payload_json=encoded, saved_at=utc_now_iso(), revision=expected + 1, schema_version=1)
            if row is None:
                if not self.database.insert_if_absent(session, PublicationDraftModel, dict(**scope, **values)):
                    raise DraftConflict('draft changed; read before saving')
            else:
                result = session.execute(update(PublicationDraftModel).where(PublicationDraftModel.id == row.id, PublicationDraftModel.revision == expected).values(**values))
                if result.rowcount != 1:
                    raise DraftConflict('draft changed; read before saving')
            session.expire_all()
            return self._view(session.scalar(select(PublicationDraftModel).filter_by(**scope)))
