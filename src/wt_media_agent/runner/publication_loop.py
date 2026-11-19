"""Serial Local Agent publication polling. Signed authority stays with Cloud."""
from __future__ import annotations

import logging
from threading import Event

from wt_media_agent.runtime.agent_token import routing_user_id
from wt_media_agent.clients.cloud.errors import AgentAuthError
from wt_media_agent.runtime.constants import DEFAULT_PUBLICATION_POLL_SECONDS

logger = logging.getLogger(__name__)




class PublicationLoop:
    def __init__(self, agent_token, preparation, job, *, interval=DEFAULT_PUBLICATION_POLL_SECONDS):
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
