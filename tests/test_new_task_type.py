"""AC-11: a new task type and a new external client need no change under `src/`.

The acceptance item is a claim about where change is **not** required, so the
test is shaped as the smallest thing that could falsify it: one client for a
service the Agent has never heard of, one executor that drives it through that
client, wired in through the existing public seam (`TaskRunner.register_executor`)
and run through the real claim → checkpoint → report path.

Everything below is defined in this file. If supporting a new task type still
needed a line in `runtime/`, `bootstrap/` or `runner/registry.py`, this test
could not be written without one -- and that is the falsification.

The store is real (a temporary SQLite file with the real schema), because "the
new type gets the same durability as the built-in ones" is part of what has to
hold. Only Cloud is a double, and it is a double for the same reason the
existing runner tests use one: the point here is the Agent's own dispatch, not
Cloud's.
"""

from __future__ import annotations

from collections.abc import Mapping
import sys
from pathlib import Path
import tempfile
import unittest
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from support import UnusedBitBrowser

from wt_media_agent import runtime
from wt_media_agent.runner import TaskRunner, TaskRunnerConfig
from wt_media_agent.runner.registry import default_executor_factories
from wt_media_agent.storage.checkpoint_store import CheckpointStore
from wt_media_agent.storage.migration import apply_migrations

#: Deliberately absent from `runtime/constants.py`: the whole claim is that a
#: task type can exist without one.
DEMO_TASK_TYPE = "demo_echo_task"


class DemoServiceClient:
    """A client for a service the Agent did not know about yesterday.

    It takes its endpoint from its caller, like every client under `clients/`
    does after T-04. The difference is that nothing under `src/` was edited to
    let this one exist -- no config key, no factory, no registry entry.
    """

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.calls: list[str] = []

    def echo(self, text: str) -> str:
        self.calls.append(text)
        return text.upper()


class DemoEchoExecutor:
    """Reports progress the way the built-in executors do, and nothing more.

    `probe` runs while the task is in flight, so the test can observe the
    persisted checkpoint at a moment the caller controls. Optional, and defaulted
    to a no-op, so the executor itself stays the shape a real one would have.
    """

    def __init__(
        self,
        client: object,
        agent_id: str,
        service: DemoServiceClient,
        probe: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.client = client
        self.agent_id = agent_id
        self.service = service
        self._probe = probe if probe is not None else (lambda task_id: None)

    def execute(self, task: Mapping[str, object]) -> Mapping[str, object]:
        task_id = str(task.get("task_id", ""))
        self.client.report_task(task_id, self.agent_id, "running", 0, "demo started")
        self._probe(task_id)
        echoed = self.service.echo(str(task.get("payload", "")))
        self.client.report_task(task_id, self.agent_id, "running", 50, echoed)
        return self.client.report_task(task_id, self.agent_id, "succeeded", 100, echoed)


class RecordingCloud:
    """Hands out one task, then records what the runner reported."""

    def __init__(self, task: Optional[Mapping[str, object]]) -> None:
        self.task = task
        self.reports: list[tuple[str, int, str]] = []

    def claim_task(self, agent_id: str, lease_seconds: int):
        task, self.task = self.task, None
        return task

    def report_task(self, task_id, agent_id, status, progress, message):
        self.reports.append((status, progress, message))
        return {"ok": True}


def declared_task_types() -> set[str]:
    return {
        value
        for name, value in vars(runtime.constants).items()
        if name.startswith("TASK_TYPE_") and isinstance(value, str)
    }


class NewTaskTypeTests(unittest.TestCase):
    """Builds a runner the way a caller would, then drives it once."""

    def build(self, *, register: bool = True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "agent.db"
        apply_migrations(db_path)

        self.store = CheckpointStore(db_path)
        self.cloud = RecordingCloud(
            {"task_id": "demo-1", "task_type": DEMO_TASK_TYPE, "payload": "hello"}
        )
        runner = TaskRunner(
            self.cloud,
            self.store,
            TaskRunnerConfig(
                agent_id="agent-7f3c",
                base_url="http://127.0.0.1:8188",
                db_path=str(db_path),
            ),
        )

        self.service = DemoServiceClient("https://service.example.test/v1")
        self.observed_status: list[str] = []
        self.factory_args: list[tuple[object, str]] = []

        def factory(client, agent_id):
            self.factory_args.append((client, agent_id))
            return DemoEchoExecutor(
                client,
                agent_id,
                self.service,
                probe=lambda task_id: self.observed_status.append(
                    self.store.get_checkpoint(task_id).checkpoint_status
                ),
            )

        if register:
            runner.register_executor(DEMO_TASK_TYPE, factory)
        return runner

    def test_the_demo_type_is_not_one_the_agent_already_declares(self):
        """The claim is about a *new* type, so it must not be an existing one.

        Both halves matter: the constant table must not contain it (no edit to
        `runtime/constants.py`) and the built-in registry must not contain it
        (no edit to `runner/registry.py`). The positive control keeps this from
        passing against an empty or mis-scanned table.
        """
        builtins = default_executor_factories(UnusedBitBrowser())
        self.assertGreaterEqual(len(builtins), 10, "positive control: the registry is populated")

        self.assertNotIn(DEMO_TASK_TYPE, declared_task_types())
        self.assertNotIn(DEMO_TASK_TYPE, builtins)

    def test_the_new_type_runs_end_to_end_and_is_checkpointed_like_the_builtins(self):
        runner = self.build()

        runner._poll_once()

        self.assertEqual(self.service.calls, ["hello"], "the new client was actually used")
        self.assertEqual(
            self.cloud.reports,
            [
                ("running", 0, "demo started"),
                ("running", 50, "HELLO"),
                ("succeeded", 100, "HELLO"),
            ],
        )
        # In flight, the runner had already written the checkpoint the new type
        # relies on -- not a promise made only to the built-in executors.
        self.assertEqual(self.observed_status, ["running"])
        # And completion clears it, so a restart does not resurrect a finished
        # task. Both directions, because "never wrote one" would also pass a
        # one-sided emptiness check.
        self.assertIsNone(self.store.get_checkpoint("demo-1"))
        self.assertEqual(self.store.get_incomplete_checkpoints(), [])

    def test_the_factory_receives_the_runner_s_own_client_and_the_configured_agent_id(self):
        runner = self.build()

        runner._poll_once()

        self.assertEqual(self.factory_args, [(self.cloud, "agent-7f3c")])

    def test_the_new_type_cannot_run_until_it_is_registered(self):
        """The other side of the same claim.

        Without `register_executor` the runner fails closed: the built-in
        registry that once lived inside `TaskRunner.__init__` would have let an
        unknown type through to Cloud and BitBrowser instead.
        """
        runner = self.build(register=False)

        runner._poll_once()

        self.assertEqual(self.cloud.reports, [("failed", 0, "no_executor")])
        self.assertEqual(self.service.calls, [], "an unregistered type must reach nothing")
        self.assertEqual(self.factory_args, [])


if __name__ == "__main__":
    unittest.main()
