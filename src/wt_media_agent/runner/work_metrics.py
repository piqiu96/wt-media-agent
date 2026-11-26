"""Bounded read-only metric polling. Cloud owns scheduling, scope and leases."""
import logging
from threading import Event

from wt_media_agent.clients.bilibili.metrics import MetricsError, read_metrics
from wt_media_agent.runtime.agent_token import routing_user_id

logger = logging.getLogger(__name__)


class WorkMetricsLoop:
    def __init__(self, cloud, agent_token, *, collect=read_metrics, interval=30):
        self.cloud, self.agent_token, self.collect = cloud, agent_token, collect
        self.interval = interval
        self._stop = Event()

    def tick(self):
        token = self.agent_token.get()
        if self._stop.is_set() or routing_user_id(token) is None:
            return
        claim = self.cloud.claim_work_metrics(token)
        if claim is None:
            return
        # No new platform request after the operator changes or logs out.
        if self._stop.is_set() or self.agent_token.get() != token:
            return
        body = {key: claim[key] for key in ('lease_token', 'platform_work_id', 'platform_account_uid')}
        try:
            body['counts'] = self.collect(claim['platform_work_id'], claim['platform_account_uid'])
        except MetricsError as error:
            body['error'] = error.code
        self.cloud.report_work_metrics(token, claim['work_id'], body)

    def start(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as error:
                # No tokens, platform response bodies or raw URLs in logs.
                logger.warning('work metrics deferred: error_type=%s', type(error).__name__)
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
