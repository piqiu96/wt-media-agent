"""One-shot platform boundary. Recovered attempts are never browser jobs.

Cloud claim/permission/result adapters are injected by bootstrap when available.
This component does not create clients or declare a publication successful.
"""
from __future__ import annotations
from typing import Callable
from wt_media_agent.runtime.constants import (
    PublicationPhase,
    PublicationStatus,
)
from wt_media_agent.storage.publication import ExecutionIdentity, PublicationJournal


class SubmissionBlocked(RuntimeError):
    """No fresh, explicit submit permission was received."""


class PublicationExecution:
    def __init__(self, journal: PublicationJournal):
        self.journal = journal

    def execute(self, identity: ExecutionIdentity, frozen_input: dict, *, prepare: Callable[[], None],
                preflight: Callable[[int], bool], submit: Callable[[], None], read_result: Callable[[], dict]) -> dict:
        if not self.journal.register(identity, frozen_input):
            state = self.journal.get(identity)
            # Even a crash before preparation requires recovery, never automatic re-entry.
            # There is intentionally no saved "allowed" permission or lease expiry.
            return {'status': PublicationStatus.UNCERTAIN, 'recovery_required': True, 'phase': state['phase']}
        prepare()
        self.journal.advance(identity, PublicationPhase.PREPARED)
        seq = self.journal.advance(identity, PublicationPhase.SUBMITTING)
        if preflight(seq) is not True:
            raise SubmissionBlocked('fresh submit permission was not granted')
        self.journal.advance(identity, PublicationPhase.CLICK_STARTED)
        submit()
        result = read_result()
        state = self.journal.get(identity)
        self.journal.save_result(identity, result, report_seq=state['report_seq'] + 1)
        return result


class PublicationDelivery:
    """Send original packets outside database transactions; persist each outcome."""
    def __init__(self, journal: PublicationJournal):
        self.journal = journal

    def flush(self, cloud_env: str, user_id: str, send: Callable[[dict], dict]) -> int:
        from copy import deepcopy
        delivered = 0
        for pending in self.journal.pending(cloud_env, user_id):
            packet = pending['packet']
            try:
                ack = send(deepcopy(packet))
            except Exception:
                ack = None
            accepted = isinstance(ack, dict) and (ack.get('attempt_id'), ack.get('result_hash')) == (packet['attempt_id'], packet['result_hash'])
            delivered += int(self.journal.record_delivery(cloud_env, user_id, packet, accepted=accepted))
        return delivered
