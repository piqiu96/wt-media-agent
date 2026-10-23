"""Cloud 失败应答的分类。

所有 Agent 调用共用 `{errcode, message, data, error}` 信封，`error.code` 是
机器可读的错误码。这里按调用方的下一步动作分成四类：换 Token（AgentAuthError）、
放弃本次 attempt（StaleAttemptError）、空轮询（NoPendingTaskError）、其它
（CloudError）。传输层失败单独成类，因为它不能证明 Token 或任务有任何变化。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Optional

#: 这些码意味着“这个 Token 不要再拿去 claim”：Desktop 被替换、没有有效
#: Desktop 会话、Token 过期，都要等 Desktop 重新登录换取新 Token。
AUTH_CODES = frozenset(
    {
        "AUTH_AGENT_TOKEN_INVALID",
        "AUTH_AGENT_TOKEN_EXPIRED",
        "AUTH_DESKTOP_REPLACED",
        "DESKTOP_LOGIN_REQUIRED",
        "AUTH_SESSION_INVALID",
        "AUTH_SESSION_INVALIDATED",
        "AUTH_SESSION_EXPIRED",
        "DESKTOP_LOGIN_CONFLICT",
        "AGENT_SCOPE_FORBIDDEN",
    }
)

#: ClaimTask 在没有可领取任务时返回该 errcode，属于正常空轮询。
NO_PENDING_TASK_ERRCODE = 30001


class CloudError(RuntimeError):
    """Cloud 拒绝了调用，并给出了自己的错误码。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        recovery_action: str = "",
        errcode: int = 0,
    ) -> None:
        super().__init__(message or code or "Cloud request failed")
        self.code = code
        self.message = message
        self.retryable = retryable
        self.recovery_action = recovery_action
        self.errcode = errcode


class AgentAuthError(CloudError):
    """Agent Token 或它绑定的 Desktop 会话已不被接受。"""


class StaleAttemptError(CloudError):
    """任务已越过本次 attempt，结果不能覆盖它。"""


class NoPendingTaskError(CloudError):
    """claim 没有找到可领取的任务。"""


class CloudUnavailableError(RuntimeError):
    """没有连上 Cloud，或应答不是 Cloud 的信封。"""


def failure_from(
    response: Mapping[str, object], http_status: Optional[int] = None
) -> Optional[CloudError]:
    """把一条 Cloud 信封转成对应的失败类型，成功时返回 None。

    errcode 先于 HTTP 状态判断：claim 的空轮询是 409 + 30001 且不带
    `error.code`，传输面的 409 也不带 `error`，只看状态会把它们归错类。
    """
    errcode = response.get("errcode")
    errcode = errcode if isinstance(errcode, int) else 0
    succeeded = errcode == 0 and (http_status is None or 200 <= http_status < 300)
    if succeeded:
        return None

    error = response.get("error")
    error = error if isinstance(error, Mapping) else {}
    code = str(error.get("code") or error.get("type") or "")
    message = str(response.get("message") or error.get("message") or "")
    kwargs = {
        "retryable": bool(error.get("retryable")),
        "recovery_action": str(error.get("recovery_action") or ""),
        "errcode": errcode,
    }

    if errcode == NO_PENDING_TASK_ERRCODE:
        return NoPendingTaskError(code or "NO_PENDING_TASK", message, **kwargs)
    if code == "STALE_ATTEMPT":
        return StaleAttemptError(code, message, **kwargs)
    if code in AUTH_CODES or http_status == 401:
        return AgentAuthError(code or "AUTH_AGENT_TOKEN_INVALID", message, **kwargs)
    return CloudError(code, message, **kwargs)
