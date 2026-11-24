"""Bounded independent publication calls; Cloud owns fixed groups and authority."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from threading import BoundedSemaphore, Event, Lock, RLock, Thread, current_thread

from wt_media_agent.runtime.agent_token import routing_user_id
from wt_media_agent.clients.cloud.errors import AgentAuthError, CloudError
from wt_media_agent.runtime.constants import DEFAULT_PUBLICATION_POLL_SECONDS

logger = logging.getLogger(__name__)


class PublicationAutomaticCapacity:
    """Local publication bounds include old owners until proven stopped."""
    def __init__(self, limit, count, *, lock=None):
        self.limit, self.count = limit, count
        self.lock = lock if lock is not None else RLock()
        self._parked, self._resuming = set(), set()

    @staticmethod
    def _key(identity):
        return getattr(identity, 'key', identity)

    def available(self):
        with self.lock:
            count = self.count(frozenset(self._parked))
            if type(count) is not int or count < 0:
                raise ValueError('automatic publication capacity is unavailable')
            return count < self.limit

    def park(self, identity):
        with self.lock:
            key = self._key(identity)
            self._resuming.discard(key)
            self._parked.add(key)

    @contextmanager
    def resume(self, identity):
        with self.lock:
            key = self._key(identity)
            admitted = key in self._resuming
            original_unknown = admitted
            if not admitted and key in self._parked and self.available():
                # The durable original claim now counts again before the HTTP
                # request. A missing response never returns its automatic slot.
                self._parked.remove(key)
                self._resuming.add(key)
                admitted = True
        try:
            yield admitted
        except CloudError as error:
            # Only the first request's explicit no-admission result proves it
            # never resumed. A retry after a lost reply cannot erase that doubt.
            if admitted and not original_unknown and error.code == 'publication_capacity_wait':
                self.park(identity)
            raise

    def finished(self, identity):
        with self.lock:
            key = self._key(identity)
            self._parked.discard(key)
            self._resuming.discard(key)


class PublicationLoop:
    def __init__(self, agent_token, preparation, job, check_job, *, window_count,
                 max_workers=10, max_automatic=None, automatic_count=None, interval=DEFAULT_PUBLICATION_POLL_SECONDS, thread_factory=Thread):
        if interval <= 0 or type(max_workers) is not int or not 1 <= max_workers <= 64:
            raise ValueError('publication polling and bounded window capacity are required')
        self.agent_token, self.preparation, self.job, self.check_job = agent_token, preparation, job, check_job
        self.interval, self.max_workers = interval, max_workers
        self.window_count = window_count
        self._thread_factory = thread_factory
        self._stop = Event()
        self._refused_token = None
        self._poll_lock, self._claim_lock, self._state_lock = Lock(), RLock(), RLock()
        max_automatic = max_workers if max_automatic is None else max_automatic
        if type(max_automatic) is not int or not 1 <= max_automatic <= 64:
            raise ValueError('bounded automatic publication capacity is required')
        self.automatic_admission = PublicationAutomaticCapacity(
            max_automatic, automatic_count if automatic_count is not None else lambda excluded: window_count(), lock=self._claim_lock)
        self.job.automatic_admission = self.automatic_admission
        self._slots = BoundedSemaphore(max_workers)
        self._threads = set()
        self._single = {'recovery': False, 'check': False}
        self._active_keys = set()
        self._serial = 0
        self.check_job.admission_guard = lambda: self.admission_guard(automatic=False)

    @contextmanager
    def admission_guard(self, *, automatic=True):
        # Hold only while claiming and durably accepting a new identity, never
        # across a platform call. Old unknown windows in every local scope count.
        with self._claim_lock:
            count = self.window_count()
            if type(count) is not int or count < 0:
                raise ValueError('retained window count is unavailable')
            yield not self._stop.is_set() and count < self.max_workers and (not automatic or self.automatic_admission.available())

    def _spawn(self, kind, operation, token, *, reserved=False, key=None):
        window = kind != 'recovery'
        if window and not reserved and not self._slots.acquire(blocking=False):
            return False
        with self._state_lock:
            if self._stop.is_set() or (kind in self._single and self._single[kind]) or (key is not None and key in self._active_keys):
                if window:
                    self._slots.release()
                return False
            if kind in self._single:
                self._single[kind] = True
            if key is not None:
                self._active_keys.add(key)
            self._serial += 1
            def execute():
                try:
                    operation()
                except AgentAuthError:
                    self._refused_token = token
                except Exception as error:
                    logger.warning('publication %s deferred: error_type=%s', kind, type(error).__name__)
                finally:
                    with self._state_lock:
                        self._threads.discard(thread)
                        if kind in self._single:
                            self._single[kind] = False
                        if key is not None:
                            self._active_keys.discard(key)
                    if key is not None:
                        self.automatic_admission.finished(key)
                    if window:
                        self._slots.release()
            thread = self._thread_factory(target=execute, name=f'publication-{kind}-{self._serial}', daemon=True)
            self._threads.add(thread)
        try:
            thread.start()
        except BaseException:
            with self._state_lock:
                self._threads.discard(thread)
                if kind in self._single:
                    self._single[kind] = False
                if key is not None:
                    self._active_keys.discard(key)
            if window:
                self._slots.release()
            raise
        return True

    def tick(self):
        if self._stop.is_set() or not self._poll_lock.acquire(blocking=False):
            return
        try:
            token = self.agent_token.get()
            user = routing_user_id(token)
            if user is None:
                return
            current = lambda: (not self._stop.is_set() and token != self._refused_token
                               and self.agent_token.get() == token and routing_user_id(token) == user)
            # Recovery never waits for a human scene or consumes a browser slot.
            def recover():
                self.job.recover(token, user)
                self.check_job.tick(token, user, current=current, recovery_only=True)
            self._spawn('recovery', recover, token)
            seen = set()
            for _ in range(self.max_workers):
                if not current() or not self._slots.acquire(blocking=False):
                    break
                reserved = True
                try:
                    with self.admission_guard() as admitted:
                        if not admitted or not current():
                            break
                        claimed = self.preparation.claim(token, user)
                    if claimed is None:
                        break
                    claim, identity = claimed
                    key = getattr(identity, 'key', identity)
                    if key in seen:
                        break
                    seen.add(key)
                    # A claim is never queued behind a full executor. Each
                    # original browser/Playwright call stays on this one thread.
                    reserved = False
                    self._spawn('run', lambda c=claim, i=identity: self.job.run(token, c, i, current=current),
                                token, reserved=True, key=key)
                finally:
                    if reserved:
                        self._slots.release()
            # tick first reconciles durable checks even after new-work refusal.
            self._spawn('check', lambda: self.check_job.tick(token, user, current=current), token)
        except AgentAuthError:
            self._refused_token = token
        except Exception as error:
            logger.warning('publication polling deferred: error_type=%s', type(error).__name__)
        finally:
            self._poll_lock.release()

    def start(self):
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
        with self._state_lock:
            threads = tuple(self._threads)
        for thread in threads:
            if thread is not current_thread():
                thread.join(timeout=.2)
