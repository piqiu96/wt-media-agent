"""Built-in task-type to executor-factory registry.

Each factory takes `(client, agent_id)` and returns an executor instance, so
`bootstrap` can bind anything else it owns (such as the BitBrowser client) by
closure without changing the factory arity.
"""

from __future__ import annotations

from wt_media_agent.executors.account_check import AccountCheckExecutor
from wt_media_agent.executors.cookie import CookieReadExecutor, CookieWriteExecutor
from wt_media_agent.executors.noop import NoopExecutor
from wt_media_agent.executors.profile import factory as profile_executor_factory
from wt_media_agent.executors.protocol import ExecutorFactory
from wt_media_agent.executors.proxy import ProxyCheckExecutor
from wt_media_agent.executors.proxy_mutation import ProxyMutationExecutor
from wt_media_agent.runtime.constants import (
    TASK_TYPE_ACCOUNT_CHECK,
    TASK_TYPE_COOKIE_READ,
    TASK_TYPE_COOKIE_WRITE,
    TASK_TYPE_NOOP,
    TASK_TYPE_PROFILE_CLOSE,
    TASK_TYPE_PROFILE_CREATE,
    TASK_TYPE_PROFILE_OPEN,
    TASK_TYPE_PROFILE_UPDATE,
    TASK_TYPE_PROXY_CHECK,
    TASK_TYPE_PROXY_MUTATION,
)


def default_executor_factories() -> dict[str, ExecutorFactory]:
    """Return the built-in task-type to executor-factory mapping."""
    return {
        TASK_TYPE_NOOP: lambda c, a: NoopExecutor(c, a),
        TASK_TYPE_COOKIE_READ: lambda c, a: CookieReadExecutor(c, a),
        TASK_TYPE_COOKIE_WRITE: lambda c, a: CookieWriteExecutor(c, a),
        TASK_TYPE_ACCOUNT_CHECK: lambda c, a: AccountCheckExecutor(c, a),
        TASK_TYPE_PROFILE_CREATE: profile_executor_factory("create"),
        TASK_TYPE_PROFILE_OPEN: profile_executor_factory("open"),
        TASK_TYPE_PROFILE_CLOSE: profile_executor_factory("close"),
        TASK_TYPE_PROFILE_UPDATE: profile_executor_factory("update"),
        TASK_TYPE_PROXY_CHECK: lambda c, a: ProxyCheckExecutor(c, a),
        TASK_TYPE_PROXY_MUTATION: lambda c, a: ProxyMutationExecutor(c, a),
    }
