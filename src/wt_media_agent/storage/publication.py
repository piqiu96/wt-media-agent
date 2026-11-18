"""Durable publication boundary, using the existing checkpoint/outbox tables.

These are local records, not a second Cloud task model or a wire contract.
A report acknowledgement never authorizes another platform click.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, parse_qs

from wt_media_agent.storage.sqlite import connect
from wt_media_agent.utils.time import utc_now_iso


class JournalConflict(RuntimeError):
    """An immutable input, active profile or result would be overwritten."""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _environment(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('a credential-free Cloud environment URL is required')
    host = parsed.hostname.lower()
    if ':' in host:
        host = '[' + host + ']'
    port = parsed.port
    if port is not None and (parsed.scheme, port) not in (('http', 80), ('https', 443)):
        host += ':' + str(port)
    if any(part in ('.', '..') for part in parsed.path.split('/')):
        raise ValueError('ambiguous environment path')
    return parsed.scheme + '://' + host + parsed.path.rstrip('/')



@dataclass(frozen=True)
class ExecutionIdentity:
    environment: str
    user_id: str
    task_id: str
    attempt_id: str
    bit_profile_id: str

    def __post_init__(self):
        object.__setattr__(self, 'environment', _environment(self.environment))
        for value in (self.user_id, self.task_id, self.attempt_id):
            if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or str(int(value)) != value or not 0 < int(value) < 2**64:
                raise ValueError('positive canonical decimal identities are required')
        if not isinstance(self.bit_profile_id, str) or not self.bit_profile_id or len(self.bit_profile_id) > 255 or self.bit_profile_id.strip() != self.bit_profile_id:
            raise ValueError('actual BitBrowser Profile identity is required')

    @property
    def key(self) -> str:
        # Attempt is the local unit. Task ids can overlap downloads and other Clouds.
        return 'publication:' + hashlib.sha256(_json([self.environment, self.user_id, self.attempt_id]).encode()).hexdigest()


def _validated_snapshot(value: dict) -> str:
    required = {'platform', 'platform_uid', 'video_sha256', 'video_size', 'cover_sha256', 'cover_size', 'content'}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError('only immutable public execution inputs may be saved')
    if value['platform'] not in ('baidu', 'bilibili', 'xiaohongshu') or not isinstance(value['platform_uid'], str) or not value['platform_uid']:
        raise ValueError('platform identity is required')
    for name in ('video_sha256', 'cover_sha256'):
        digest = value[name]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('file digest is required')
    for name in ('video_size', 'cover_size'):
        if type(value[name]) is not int or value[name] <= 0:
            raise ValueError('positive file size is required')
    content = value['content']
    if not isinstance(content, dict) or set(content) != {'description', 'topics', 'declaration'} or any(not isinstance(v, str) for v in content.values()):
        raise ValueError('only confirmed text fields may be saved')
    return _json(value)


def _safe_result(value: dict) -> None:
    if not isinstance(value, dict) or value.get('status') not in ('reviewing', 'failed', 'uncertain'):
        raise ValueError('an explicit observation is required')
    def visit(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if not isinstance(key, str) or any(part in key.lower() for part in ('token', 'cookie', 'password', 'authorization', 'download_url', 'signed_url')):
                    raise ValueError('credentials and signed addresses cannot enter evidence')
                visit(child)
        elif isinstance(node, list):
            for child in node: visit(child)
        elif isinstance(node, str) and node.startswith(('https://', 'http://')):
            url = urlsplit(node)
            public_id_query = False
            if url.scheme == 'https' and url.netloc == 'baijiahao.baidu.com' and url.path == '/s':
                query = parse_qs(url.query, keep_blank_values=True)
                ids = query.get('id', [])
                public_id_query = set(query) == {'id'} and len(ids) == 1 and ids[0].isascii() and ids[0].isdecimal()
            if url.scheme != 'https' or url.username or url.password or (url.query and not public_id_query) or url.fragment:
                raise ValueError('only public credential-free evidence URLs may be saved')
    visit(value)


class PublicationJournal:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    @contextmanager
    def _transaction(self):
        with closing(connect(self.db_path)) as db:
            # The submit barrier must be durable before an external side effect.
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            with db:
                yield db

    @staticmethod
    def _state(db, identity):
        row = db.execute('SELECT * FROM task_checkpoints WHERE task_id=? AND task_type=?', (identity.key, 'publication')).fetchone()
        if row is None:
            raise JournalConflict('no durable publication claim')
        state = json.loads(row['checkpoint_json'])
        if state['identity'] != asdict(identity):
            raise JournalConflict('attempt identity differs')
        return row, state

    @staticmethod
    def _write(db, identity, state):
        db.execute('UPDATE task_checkpoints SET checkpoint_json=?,last_stage=?,updated_at=? WHERE task_id=?',
                   (_json(state), state['phase'], utc_now_iso(), identity.key))

    def register(self, identity: ExecutionIdentity, frozen_input: dict) -> bool:
        encoded = _validated_snapshot(frozen_input)
        with self._transaction() as db:
            old = db.execute('SELECT * FROM task_checkpoints WHERE task_id=?', (identity.key,)).fetchone()
            if old is not None:
                _, state = self._state(db, identity)
                if _json(state['input']) != encoded:
                    raise JournalConflict('attempt input differs')
                return False
            busy = db.execute("""SELECT 1 FROM task_checkpoints
                WHERE task_type='publication' AND checkpoint_status<>'publication_settled'
                  AND json_extract(checkpoint_json,'$.identity.bit_profile_id')=? LIMIT 1""",
                (identity.bit_profile_id,)).fetchone()
            if busy:
                raise JournalConflict('profile has an unresolved publication scene')
            state = {'identity': asdict(identity), 'input': json.loads(encoded), 'phase': 'claimed', 'report_seq': 0}
            now = utc_now_iso()
            db.execute("""INSERT INTO task_checkpoints(task_id,task_type,agent_id,checkpoint_status,
                attempt_id,task_payload_json,checkpoint_json,last_stage,external_side_effect,created_at,updated_at)
                VALUES(?,'publication',?,'publication_open',?,?,?,'claimed',1,?,?)""",
                (identity.key, identity.user_id, identity.attempt_id, encoded, _json(state), now, now))
            return True

    def get(self, identity: ExecutionIdentity) -> dict:
        with self._transaction() as db:
            _, state = self._state(db, identity)
            return state

    def advance(self, identity: ExecutionIdentity, phase: str) -> int:
        previous = {'prepared': 'claimed', 'submitting': 'prepared', 'click_started': 'submitting'}
        if phase not in previous:
            raise ValueError('unsupported local submit phase')
        with self._transaction() as db:
            _, state = self._state(db, identity)
            if state['phase'] != previous[phase]:
                raise JournalConflict('submit boundary cannot be replayed or reversed')
            state['phase'] = phase
            state['report_seq'] += 1
            self._write(db, identity, state)
            return state['report_seq']

    def reserve_report(self, identity: ExecutionIdentity) -> int:
        """Reserve an observation sequence without changing the submit boundary."""
        with self._transaction() as db:
            _, state = self._state(db, identity)
            if state['phase'] not in ('claimed', 'prepared'):
                raise JournalConflict('new observations cannot reopen a submission')
            state['report_seq'] += 1
            self._write(db, identity, state)
            return state['report_seq']

    def unresolved_results(self, environment: str, user_id: str) -> list[dict]:
        """Read original packets, including acknowledged-but-not-settled crashes."""
        with self._transaction() as db:
            rows = db.execute("""SELECT checkpoint_json,final_result_json FROM task_checkpoints
                WHERE task_type='publication' AND checkpoint_status<>'publication_settled'
                  AND agent_id=? AND json_extract(checkpoint_json,'$.identity.environment')=?
                  AND final_result_json IS NOT NULL ORDER BY task_id""",
                (user_id, _environment(environment))).fetchall()
            return [{'identity': ExecutionIdentity(**json.loads(row['checkpoint_json'])['identity']),
                     'packet': json.loads(row['final_result_json'])} for row in rows]

    def save_result(self, identity: ExecutionIdentity, result: dict, *, report_seq: int) -> dict:
        _safe_result(result)
        if type(report_seq) is not int or report_seq <= 0:
            raise JournalConflict('result sequence must advance')
        # Provider-defined canonical UTF-8 hash. Persist and resend this exact packet.
        digest = hashlib.sha256(_json({'report_seq': report_seq, 'result': result}).encode()).hexdigest()
        packet = {'task_id': identity.task_id, 'attempt_id': identity.attempt_id, 'report_seq': report_seq, 'result_hash': digest, 'result': result}
        with self._transaction() as db:
            row, state = self._state(db, identity)
            if row['final_result_json'] is not None:
                prior = json.loads(row['final_result_json'])
                if prior != packet:
                    raise JournalConflict('saved execution result is immutable')
                return prior
            if report_seq <= state['report_seq']:
                raise JournalConflict('result sequence is stale')
            state.update(phase='result_saved', report_seq=report_seq, result_hash=digest)
            self._write(db, identity, state)
            now = utc_now_iso()
            db.execute("""UPDATE task_checkpoints SET checkpoint_status='publication_result',
                final_status=?,final_result_json=?,report_pending=1,finished_at=? WHERE task_id=?""",
                (result['status'], _json(packet), now, identity.key))
            db.execute("""INSERT INTO offline_results(task_id,agent_id,attempt_id,status,result_json,queued_at)
                VALUES(?,?,?,?,?,?)""", (identity.key, identity.user_id, identity.attempt_id, result['status'], _json(packet), now))
        return packet

    def pending(self, environment: str, user_id: str) -> list[dict]:
        with self._transaction() as db:
            rows = db.execute("""SELECT r.id,r.result_json FROM offline_results r
                JOIN task_checkpoints c ON c.task_id=r.task_id
                WHERE c.task_type='publication' AND r.delivered=0 AND c.agent_id=?
                  AND json_extract(c.checkpoint_json,'$.identity.environment')=? ORDER BY r.id""",
                (user_id, _environment(environment))).fetchall()
            return [{'id': row['id'], 'packet': json.loads(row['result_json'])} for row in rows]

    def flush(self, environment: str, user_id: str, send: Callable[[dict], dict], *, only_attempt: str | None = None) -> int:
        delivered = 0
        for pending in self.pending(environment, user_id):
            # Network is outside the transaction. A lost acknowledgement simply resends this packet.
            packet = pending['packet']
            if only_attempt is not None and packet['attempt_id'] != only_attempt:
                continue
            expected = (packet['attempt_id'], packet['result_hash'])
            try:
                ack = send(json.loads(_json(packet)))
            except Exception:
                ack = None
            accepted = isinstance(ack, dict) and (ack.get('attempt_id'), ack.get('result_hash')) == expected
            with self._transaction() as db:
                row = db.execute("""SELECT r.task_id,r.delivered,r.result_json FROM offline_results r
                    JOIN task_checkpoints c ON c.task_id=r.task_id
                    WHERE r.id=? AND c.task_type='publication' AND c.agent_id=?
                      AND json_extract(c.checkpoint_json,'$.identity.environment')=?""",
                    (pending['id'], user_id, _environment(environment))).fetchone()
                if row is None or row['delivered']:
                    continue
                if json.loads(row['result_json']) != pending['packet']:
                    raise JournalConflict('pending result differs')
                if accepted:
                    db.execute('UPDATE offline_results SET delivered=1,delivered_at=? WHERE id=?', (utc_now_iso(), pending['id']))
                    db.execute('UPDATE task_checkpoints SET report_pending=0 WHERE task_id=?', (row['task_id'],))
                    delivered += 1
                else:
                    db.execute('UPDATE offline_results SET delivery_attempts=delivery_attempts+1 WHERE id=?', (pending['id'],))
        return delivered

    def settle(self, identity: ExecutionIdentity, result_hash: str, *, task_status: str):
        """Called only after provider-confirmed resolution and safe browser cleanup.

        No clock, successful report, or local observation can invoke this implicitly.
        The frozen attempt remains as a replay barrier after releasing its Profile.
        """
        if task_status not in ('published', 'reviewing', 'failed', 'cancelled', 'abandoned'):
            raise JournalConflict('Cloud has not resolved this execution')
        with self._transaction() as db:
            row, state = self._state(db, identity)
            if not state.get('result_hash') or state['result_hash'] != result_hash or row['report_pending']:
                raise JournalConflict('result must be durably acknowledged before resolution')
            state['phase'] = 'settled'
            self._write(db, identity, state)
            db.execute("UPDATE task_checkpoints SET checkpoint_status='publication_settled' WHERE task_id=?", (identity.key,))
