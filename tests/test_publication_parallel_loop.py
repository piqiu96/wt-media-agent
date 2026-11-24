"""Bounded real threads: a parked call cannot serialize independent Profiles."""
import threading
import unittest
from types import SimpleNamespace

from test_publication_loop import token
from wt_media_agent.runner.publication_loop import PublicationLoop
from wt_media_agent.runtime.agent_token import AgentToken


class ParallelPublicationLoopTests(unittest.TestCase):
    def make_loop(self, total, *, windows=10, held=lambda: 0):
        self.release = threading.Event()
        self.recovered = threading.Event()
        self.checked = threading.Event()
        self.entered = [threading.Event() for _ in range(total)]
        self.claimed = []
        self.current = []
        def claim(t, u):
            index = len(self.claimed)
            if index >= total:
                return None
            self.claimed.append(index)
            return {'task': index}, index
        def run(t, c, i, *, current):
            self.current.append(current)
            self.entered[i].set()
            self.release.wait(3)
        holder = AgentToken(); holder.set(token())
        loop = PublicationLoop(holder, SimpleNamespace(claim=claim),
            SimpleNamespace(run=run, recover=lambda *a: self.recovered.set()),
            SimpleNamespace(tick=lambda *a, **kw: self.checked.set()),
            max_workers=windows, window_count=held)
        self.addCleanup(self.release.set)
        self.addCleanup(loop.stop)
        return loop

    def test_group_dispatch_has_no_per_task_poll_delay(self):
        loop = self.make_loop(2, windows=3)
        loop.tick()
        self.assertTrue(all(event.wait(1) for event in self.entered))
        self.assertTrue(self.checked.wait(1))
        self.assertTrue(self.recovered.wait(1))
        self.assertFalse(self.release.is_set())

    def test_five_parked_calls_do_not_exhaust_a_ten_window_pool(self):
        loop = self.make_loop(6, windows=10)
        loop.tick()
        self.assertTrue(all(event.wait(1) for event in self.entered))
        self.assertFalse(self.release.is_set())

    def test_full_live_window_pool_never_claims_a_queued_job(self):
        loop = self.make_loop(3, windows=2)
        loop.tick()
        self.assertTrue(self.entered[0].wait(1)); self.assertTrue(self.entered[1].wait(1))
        for _ in range(3):
            loop.tick()
        self.assertEqual(self.claimed, [0, 1])
        self.assertFalse(self.entered[2].is_set())
        self.assertTrue(self.recovered.wait(1))

    def test_unknown_windows_from_previous_owner_block_new_claims_but_not_recovery(self):
        loop = self.make_loop(1, windows=2, held=lambda: 2)
        loop.tick()
        self.assertEqual(self.claimed, [])
        self.assertTrue(self.recovered.wait(1))

    def test_stop_revokes_all_live_original_calls(self):
        loop = self.make_loop(2, windows=3)
        loop.tick()
        self.assertTrue(all(event.wait(1) for event in self.entered))
        self.assertTrue(all(check() for check in self.current))
        loop.stop()
        self.assertTrue(all(not check() for check in self.current))
