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

    def test_full_window_pool_still_reconciles_original_check_without_new_work(self):
        loop = self.make_loop(2, windows=2)
        reconciled = threading.Event()
        def check(*args, current, recovery_only=False):
            if recovery_only and current(): reconciled.set()
        loop.check_job.tick = check
        loop.tick()
        self.assertTrue(all(event.wait(1) for event in self.entered))
        reconciled.clear()
        loop.tick()
        self.assertTrue(reconciled.wait(1))

    def test_automatic_capacity_is_shared_after_switching_owner(self):
        from wt_media_agent.runner.publication_loop import PublicationAutomaticCapacity
        held = {'A1', 'A2', 'A3', 'A4', 'A5'}
        gate = PublicationAutomaticCapacity(5, lambda parked: len(held - parked))
        self.assertFalse(gate.available())
        # Only a reliably parked original scene may free a local automatic slot.
        gate.park('A1')
        self.assertTrue(gate.available())
        held.add('B1')
        self.assertFalse(gate.available())
        with gate.resume('A1') as admitted:
            self.assertFalse(admitted)
        held.remove('B1')
        with gate.resume('A1') as admitted:
            self.assertTrue(admitted)
        # A lost resume reply retains the original reservation; replay does
        # not spend a second automatic slot or permit a sixth operation.
        with gate.resume('A1') as admitted:
            self.assertTrue(admitted)
        self.assertFalse(gate.available())
        gate.finished('A1')
        self.assertFalse(gate.available())  # unknown old work remains held
        held.remove('A1')
        self.assertTrue(gate.available())

    def test_check_needs_window_capacity_and_does_not_spend_publish_automatic_slots(self):
        loop = self.make_loop(0, windows=10, held=lambda:1)
        # One publication's automatic slot is full; an independent read-only
        # check still fits the shared window and Profile bounds.
        loop.automatic_admission.limit = 1
        loop.automatic_admission.count = lambda parked:1
        with loop.admission_guard() as admitted:
            self.assertFalse(admitted)
        with loop.check_job.admission_guard() as admitted:
            self.assertTrue(admitted)
        # A retained check consumes a window, not a publication automatic slot.
        loop.automatic_admission.count = lambda parked:0
        with loop.admission_guard() as admitted:
            self.assertTrue(admitted)
        loop.window_count = lambda:10
        with loop.check_job.admission_guard() as admitted:
            self.assertFalse(admitted)

    def test_explicit_first_resume_capacity_rejection_returns_only_its_local_slot(self):
        from wt_media_agent.runner.publication_loop import PublicationAutomaticCapacity
        from wt_media_agent.clients.cloud.errors import CloudError
        held = {'A'}
        gate = PublicationAutomaticCapacity(1,lambda parked:len(held-parked))
        gate.park('A')
        with self.assertRaises(CloudError):
            with gate.resume('A') as admitted:
                self.assertTrue(admitted)
                raise CloudError('publication_capacity_wait','full')
        self.assertTrue(gate.available())
        # An uncertain previous request cannot be disproved by a later refusal.
        with self.assertRaises(ConnectionError):
            with gate.resume('A') as admitted:
                self.assertTrue(admitted)
                raise ConnectionError('lost reply')
        with self.assertRaises(CloudError):
            with gate.resume('A') as admitted:
                self.assertTrue(admitted)
                raise CloudError('publication_capacity_wait','full')
        self.assertFalse(gate.available())

    def test_first_authoritative_resume_rejections_do_not_leak_automatic_capacity(self):
        from wt_media_agent.runner.publication_loop import PublicationAutomaticCapacity
        from wt_media_agent.clients.cloud.errors import CloudError
        for code in ('publication_batch_parent_blocked','publication_conflict',
                     'publication_input_changed','publication_profile_use_conflict',
                     'publication_admission_closed','publication_agent_upgrade_required'):
            with self.subTest(code=code):
                gate = PublicationAutomaticCapacity(1,lambda parked:len({'A'}-parked))
                gate.park('A')
                with self.assertRaises(CloudError):
                    with gate.resume('A') as admitted:
                        self.assertTrue(admitted)
                        raise CloudError(code,'not applied')
                self.assertTrue(gate.available())

    def test_real_sqlite_old_owner_workers_keep_automatic_slots_after_token_change(self):
        import tempfile
        from pathlib import Path
        from dataclasses import replace
        from wt_media_agent.storage.database import Database
        from wt_media_agent.storage.migration import apply_migrations
        from wt_media_agent.storage.publication import PublicationJournal
        from test_publication_execution import identity, snapshot
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'agent.sqlite3'; apply_migrations(path)
            database = Database(path); journal = PublicationJournal(database)
            held, entered = [], [threading.Event() for _ in range(5)]
            release = threading.Event(); holder = AgentToken(); holder.set(token())
            def claim(t, user):
                i = len(held)
                owner = replace(identity(),user_id=user,task_id=str(100+i),attempt_id=str(100+i),bit_profile_id=f'profile-{i}')
                journal.register(owner,snapshot()); held.append(owner)
                return {}, owner
            def run(t, c, owner, *, current):
                index = int(owner.task_id)-100
                if index < len(entered): entered[index].set()
                release.wait(3)
            loop = PublicationLoop(holder,SimpleNamespace(claim=claim),
                SimpleNamespace(run=run,recover=lambda *args:None),
                SimpleNamespace(tick=lambda *args,**kwargs:None),window_count=journal.retained_window_count,
                max_workers=10,max_automatic=5,
                automatic_count=lambda parked:journal.retained_window_count(excluded_identities=parked))
            try:
                loop.tick(); self.assertTrue(all(event.wait(1) for event in entered))
                self.assertEqual(len(held),5)
                holder.set(token(user=8)); loop.tick()
                self.assertEqual(len(held),5)
                self.assertEqual(journal.retained_window_count(),5)
            finally:
                release.set(); loop.stop(); database.close()

    def test_stop_revokes_all_live_original_calls(self):
        loop = self.make_loop(2, windows=3)
        loop.tick()
        self.assertTrue(all(event.wait(1) for event in self.entered))
        self.assertTrue(all(check() for check in self.current))
        loop.stop()
        self.assertTrue(all(not check() for check in self.current))
