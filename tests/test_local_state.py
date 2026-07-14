from __future__ import annotations

import unittest

from wt_media_agent.local_api.state import LocalAgentState, PendingResultQueue


class LocalStateTest(unittest.TestCase):
    def test_pending_result_queue_drains(self) -> None:
        queue = PendingResultQueue()
        queue.add({"task_id": "task-1", "status": "succeeded"})

        self.assertEqual(queue.size(), 1)
        self.assertEqual(queue.drain(), [{"task_id": "task-1", "status": "succeeded"}])
        self.assertEqual(queue.size(), 0)

    def test_state_snapshot_counts_pending_results(self) -> None:
        state = LocalAgentState(agent_id="agent-1")
        state.pending_results.add({"task_id": "task-1"})

        self.assertEqual(state.snapshot()["pending_result_count"], 1)


if __name__ == "__main__":
    unittest.main()
