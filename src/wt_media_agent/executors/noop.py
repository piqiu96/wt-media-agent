"""Noop executor used to prove the M1 task execution spine."""

from __future__ import annotations

from collections.abc import Mapping

from wt_media_agent.executors.protocol import TaskReporter


class NoopExecutor:
    def __init__(self, reporter: TaskReporter, agent_id: str) -> None:
        self.reporter = reporter
        self.agent_id = agent_id

    def execute(self, task: Mapping[str, object]) -> Mapping[str, object]:
        task_id = task.get("task_id")
        task_type = task.get("task_type")
        if not isinstance(task_id, str) or task_type != "noop_task":
            raise ValueError("noop executor requires a noop_task with task_id")

        self.reporter.report_task(task_id, self.agent_id, "running", 0, "noop started")
        self.reporter.report_task(task_id, self.agent_id, "running", 50, "noop progress")
        return self.reporter.report_task(
            task_id, self.agent_id, "succeeded", 100, "noop succeeded"
        )
