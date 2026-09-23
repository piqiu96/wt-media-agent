"""Tests for the executor registry and the runner's injected registry (CHG-056 T-04).

Two properties this file exists to hold:

1. The BitBrowser client bound into the registry is the *same object* every
   browser-driving executor receives. Before T-04 each executor built its own
   from the environment, so "the executor uses the configured client" was a
   convention with no seam to assert against.
2. An unwired runner fails closed. `TaskRunner` used to build the default
   registry itself inside `__init__`, which meant any runner anywhere could
   reach Cloud and BitBrowser without the caller ever deciding that. Now the
   registry arrives as an argument and the un-wired case takes the `no_executor`
   path: no report of success, no browser call.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wt_media_agent import runtime
from wt_media_agent.runner import registry as registry_module
from wt_media_agent.runner.config import TaskRunnerConfig
from wt_media_agent.runner.registry import default_executor_factories
from wt_media_agent.runner.runner import TaskRunner

# Task types whose executor drives a BitBrowser Profile. The other two are
# deliberately client-free: `noop_task` does nothing and `proxy_check_task`
# only does outbound network probes.
BROWSER_DRIVEN = (
    "TASK_TYPE_COOKIE_READ",
    "TASK_TYPE_COOKIE_WRITE",
    "TASK_TYPE_ACCOUNT_CHECK",
    "TASK_TYPE_PROFILE_CREATE",
    "TASK_TYPE_PROFILE_OPEN",
    "TASK_TYPE_PROFILE_CLOSE",
    "TASK_TYPE_PROFILE_UPDATE",
    "TASK_TYPE_PROXY_MUTATION",
)


def declared_task_types() -> set[str]:
    """Every `TASK_TYPE_*` value the constants module declares."""
    return {
        value
        for name, value in vars(runtime.constants).items()
        if name.startswith("TASK_TYPE_") and isinstance(value, str)
    }


def task_type_of(constant_name: str) -> str:
    return getattr(runtime.constants, constant_name)


class RegistryCoverageTests(unittest.TestCase):
    def setUp(self):
        self.bitbrowser = MagicMock()
        self.factories = default_executor_factories(self.bitbrowser)
        self.client = MagicMock()

    def test_the_registry_covers_every_declared_task_type(self):
        declared = declared_task_types()
        # Positive control: an empty or mis-scanning declaration set would make
        # the equality below vacuous.
        self.assertGreaterEqual(len(declared), 10)

        self.assertEqual(set(self.factories), declared)

    def test_every_registry_entry_is_callable_and_has_a_product_with_execute(self):
        for task_type, factory in sorted(self.factories.items()):
            with self.subTest(task_type=task_type):
                self.assertTrue(callable(factory))
                instance = factory(self.client, "agent-1")
                self.assertTrue(
                    callable(getattr(instance, "execute", None)),
                    f"{task_type} produced {instance!r} with no execute()",
                )

    def test_every_browser_driven_executor_holds_the_one_injected_client(self):
        for constant_name in BROWSER_DRIVEN:
            task_type = task_type_of(constant_name)
            with self.subTest(task_type=task_type):
                instance = self.factories[task_type](self.client, "agent-1")
                self.assertIs(instance.bitbrowser, self.bitbrowser)

    def test_no_executor_constructs_a_bitbrowser_client_of_its_own(self):
        """Executors must carry the injected object, not an equal-looking one."""
        for task_type, factory in sorted(self.factories.items()):
            instance = factory(self.client, "agent-1")
            own = getattr(instance, "bitbrowser", None)
            if own is not None and own is not self.bitbrowser:
                self.fail(f"{task_type} built its own BitBrowser client: {own!r}")

    def test_the_registry_module_does_not_import_the_bitbrowser_factory(self):
        """The self-construction helper has no business in the registry."""
        self.assertFalse(
            hasattr(registry_module, "bitbrowser_from_config"),
            "registry must receive the client, not build it",
        )


class RunnerRegistryInjectionTests(unittest.TestCase):
    """The dispatch path, driven through `_poll_once` rather than private state."""

    def build_runner(self, **kwargs):
        self.client = MagicMock()
        self.client.claim_task.return_value = {
            "task_id": "task-1",
            "task_type": "cookie_read_task",
        }
        self.store = MagicMock()
        return TaskRunner(
            self.client,
            self.store,
            TaskRunnerConfig(
                agent_id="agent-1",
                base_url="http://127.0.0.1:18080",
                db_path=":memory:",
            ),
            **kwargs,
        )

    def test_an_unwired_runner_reports_no_executor_instead_of_succeeding(self):
        runner = self.build_runner()

        runner._poll_once()

        self.client.report_task.assert_called_once_with(
            "task-1", "agent-1", "failed", 0, "no_executor"
        )
        self.store.remove_checkpoint.assert_not_called()

    def test_the_default_argument_is_not_the_built_in_registry(self):
        """A caller who forgets to wire it gets nothing, not the 10 defaults.

        If the built-in registry were still the default, this task type would
        reach a real cookie executor instead of the no_executor path.
        """
        runner = self.build_runner()

        runner._poll_once()

        self.assertNotIn(
            "succeeded", [call.args[2] for call in self.client.report_task.call_args_list]
        )

    def test_an_injected_registry_receives_the_runner_client_and_agent_id(self):
        seen: list[tuple[object, str]] = []

        def factory(client, agent_id):
            seen.append((client, agent_id))
            return MagicMock()

        runner = self.build_runner(executors={"cookie_read_task": factory})

        runner._poll_once()

        self.assertEqual(seen, [(self.client, "agent-1")])

    def test_register_executor_still_overrides_an_entry(self):
        """The pre-existing public seam keeps working."""
        sentinel = MagicMock(return_value=MagicMock())
        runner = self.build_runner()

        runner.register_executor("cookie_read_task", sentinel)
        runner._poll_once()

        sentinel.assert_called_once_with(self.client, "agent-1")


if __name__ == "__main__":
    unittest.main()
