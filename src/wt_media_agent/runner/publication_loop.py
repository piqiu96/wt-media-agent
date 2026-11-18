"""Serial Local Agent publication polling. Signed authority stays with Cloud."""
from __future__ import annotations

import base64
import json
import logging
import time
from threading import Event

from wt_media_agent.clients.cloud.errors import AgentAuthError

logger = logging.getLogger(__name__)


def routing_user_id(token: str) -> str | None:
    """Decode an untrusted routing hint, never an authorization decision.

    The v1 envelope belongs to Cloud's Agent Token contract. Every operation is
    still sent with the original token for signature/session/ownership checks.
    """
    try:
        if not isinstance(token, str) or len(token) > 8192:
            return None
        version, payload, signature = token.split('.')
        if version != 'v1' or not signature:
            return None
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        user = claims['sub']
        if (claims.get('ver') != 1 or claims.get('scope') != 'agent'
                or not isinstance(claims.get('sid'), str) or not claims['sid']
                or type(user) is not int or not 0 < user <= 9223372036854775807
                or type(claims.get('exp')) is not int or claims['exp'] <= time.time()):
            return None
        return str(user)
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None


class PublicationLoop:
    def __init__(self, agent_token, preparation, job, *, interval=5.0):
        if interval <= 0:
            raise ValueError('publication polling interval must be positive')
        self.agent_token = agent_token
        self.preparation = preparation
        self.job = job
        self.interval = interval
        self._stop = Event()
        self._refused_token = None

    def tick(self):
        if self._stop.is_set():
            return
        token = self.agent_token.get()
        user = routing_user_id(token)
        if user is None:
            return
        current = lambda: (not self._stop.is_set() and self.agent_token.get() == token
                           and routing_user_id(token) == user)
        try:
            # Report-safe recovery may finish an old attempt after the Desktop
            # session was replaced. It cannot grant browser re-entry.
            self.job.recover(token, user)
            if not current() or token == self._refused_token:
                return
            claimed = self.preparation.claim(token, user)
            if claimed is not None:
                claim, identity = claimed
                self.job.run(token, claim, identity, current=current)
        except AgentAuthError:
            self._refused_token = token
        except Exception as error:
            # No platform retry here. The claim key and original journal decide
            # whether a later poll is reconciliation, never a fresh browser run.
            logger.warning('publication polling stopped this pass: error_type=%s', type(error).__name__)

    def start(self):
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
