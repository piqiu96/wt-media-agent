"""执行器协议类型。

放在 `executors/` 而不是 `runner/`：执行器和注册表都要用这些名字，而
ADR-0016 只允许 `runner -> executors` 这一个方向。

执行器拿到的是上报通道而不是 Cloud 客户端；终态如何落盘、何时上报由
Runner 决定。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Callable, Optional, Protocol


class TaskReporter(Protocol):
    """执行器可见的上报接口。"""

    def report_task(
        self,
        task_id: str,
        agent_id: str,
        status: str,
        progress: int,
        message: str = "",
        result: Optional[Mapping[str, object]] = None,
    ) -> Mapping[str, object]: ...


class Executor(Protocol):
    """A single task-type executor."""

    def execute(self, task: Mapping[str, object]) -> Mapping[str, object]: ...


ExecutorFactory = Callable[[TaskReporter, str], Executor]
