"""The surviving polling loop and the one place it is started from."""

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

from wt_media_agent.bootstrap.app import start_transfer_loop


class StubLoop:
    def __init__(self) -> None:
        self.on_threads: list[str] = []

    def start(self) -> None:
        self.on_threads.append(threading.current_thread().name)


class StartTransferLoopTests(LoggingStateTestCase):
    def test_the_transfer_loop_gets_its_own_daemon_thread(self) -> None:
        transfers = StubLoop()
        components = SimpleNamespace(transfers=transfers)

        loops = start_transfer_loop(components)
        for loop in loops:
            loop.join(timeout=5)
        self.addCleanup(lambda: [loop.join(timeout=5) for loop in loops])

        self.assertEqual([loop.name for loop in loops], ["agent-transfers"])
        self.assertTrue(all(loop.daemon for loop in loops), loops)
        self.assertEqual(transfers.on_threads, ["agent-transfers"])
        self.assertEqual([loop.is_alive() for loop in loops], [False])

    def test_the_caller_is_told_what_was_started(self) -> None:
        components = SimpleNamespace(transfers=StubLoop())

        loops = start_transfer_loop(components)
        self.addCleanup(lambda: [loop.join(timeout=5) for loop in loops])

        self.assertEqual(len(loops), 1)
        self.assertIsInstance(loops[0], threading.Thread)


if __name__ == "__main__":
    unittest.main()
