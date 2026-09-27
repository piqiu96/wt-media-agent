"""The two polling loops, and the one place they are started from.

`start_task_loops` exists because the same answer is needed in all three mode
modules and the same mistake is available in all three: `TaskRunner.start` and
`TransferRunner.start` both block until `stop()`, so one thread cannot run both,
and a mode that started only the first leaves every download claimed by nobody
while saying nothing about it. That is not hypothetical -- `bootstrap/local.py`
did exactly that until CHG-061's assembly step, while its own docstring claimed
it was the same surface the sidecar runs.

The loops are stubbed here rather than started. What is under test is what this
function starts and what it names it; a real `start()` polls Cloud until
`stop()`, so an arm that used one would leave a thread dialling an endpoint no
test serves, for the rest of the run.
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from support import LoggingStateTestCase

from wt_media_agent.bootstrap.app import start_task_loops


class StubLoop:
    """A loop that records the thread it was started on instead of polling.

    The thread's name is the whole reading: `start_task_loops` names the threads
    it creates, so a stub that recorded only "I was called" would agree with a
    function that started both loops on one thread -- which is the failure the
    two threads exist to prevent.
    """

    def __init__(self) -> None:
        self.on_threads: list[str] = []

    def start(self) -> None:
        self.on_threads.append(threading.current_thread().name)


class StartTaskLoopsTests(LoggingStateTestCase):
    """`LoggingStateTestCase` because of what this module imports, not what it does.

    Nothing here assembles the Agent, but the rule in
    `test_logging_state_isolation.py` is keyed on importing
    `wt_media_agent.bootstrap`, and a module that hand-exempted itself from it
    would be exactly what that rule cannot see. Inheriting costs a saved and
    restored logger state and keeps this file inside the scan's denominator.
    """

    def loops(self) -> tuple[SimpleNamespace, StubLoop, StubLoop]:
        runner, transfers = StubLoop(), StubLoop()
        return SimpleNamespace(runner=runner, transfers=transfers), runner, transfers

    def test_each_loop_gets_a_thread_of_its_own_with_a_name_of_its_own(self) -> None:
        components, runner, transfers = self.loops()

        loops = start_task_loops(components)
        for loop in loops:
            loop.join(timeout=5)
        self.addCleanup(lambda: [loop.join(timeout=5) for loop in loops])

        self.assertEqual([loop.name for loop in loops], ["agent-runner", "agent-transfers"])
        self.assertTrue(all(loop.daemon for loop in loops), loops)
        # Which loop ran on which thread, and not merely that both ran: swapping
        # the two `Thread(...)` arguments above would leave the names right and
        # the loops crossed, and this is the reading that separates them.
        self.assertEqual(runner.on_threads, ["agent-runner"])
        self.assertEqual(transfers.on_threads, ["agent-transfers"])
        # And a stub cannot still be running: a `start()` that blocked would
        # never have returned here, so this says the threads ended rather than
        # that they were merely created.
        self.assertEqual([loop.is_alive() for loop in loops], [False, False])

    def test_the_caller_is_told_what_was_started(self) -> None:
        """Returned, not merely started: that is what `cloud.py` joins.

        A version of this function that started the threads and returned `None`
        would run both loops correctly and leave run mode unable to wait for
        either, which is a different bug from the one the names catch.
        """
        components, _, _ = self.loops()

        loops = start_task_loops(components)
        self.addCleanup(lambda: [loop.join(timeout=5) for loop in loops])

        self.assertEqual(len(loops), 2)
        self.assertTrue(all(isinstance(loop, threading.Thread) for loop in loops), loops)


if __name__ == "__main__":
    unittest.main()
