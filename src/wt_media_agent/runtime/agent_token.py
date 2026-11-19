"""本机持有的 Agent Token，只在内存中。

Token 由 Desktop 用当前有效的 Desktop 会话向 Cloud 换取，再交给本进程；它不写
磁盘、不出现在日志里。`local_api/` 写入、`runner/` 读取，两者都不能互相依赖，
所以放在 `runtime/`。

没有清除方法：Desktop 被替换后旧 Token 在 `exp` 前仍要用于上报已领取的任务，
清除会让在途结果无法收敛。换新 Token 时直接覆盖。
"""

from __future__ import annotations

from typing import Optional


class AgentToken:
    """本进程当前使用的 Agent Token 及其过期时间（Unix 秒）。"""

    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._expires_at: Optional[int] = None

    def get(self) -> Optional[str]:
        return self._token

    def expires_at(self) -> Optional[int]:
        return self._expires_at

    def has_token(self) -> bool:
        return self._token is not None

    def set(self, token: str, expires_at: Optional[int] = None) -> None:
        if not token:
            raise ValueError("an Agent Token must not be empty")
        self._token = token
        self._expires_at = expires_at

    def __repr__(self) -> str:
        # 任何格式化或日志都不能带出 Token 本身。
        return f"<AgentToken {'set' if self._token else 'unset'}>"


import base64
import json
import time

def routing_user_id(token: str) -> str | None:
    """Decode an untrusted routing hint, never an authorization decision.

    The v1 envelope belongs to Cloud's Agent Token contract. Every operation is
    still sent with the original token for signature/session/ownership checks.
    """
    try:
        if not isinstance(token, str) or len(token) > 8192:
            return None
        version, payload, signature = token.split('.')
        if version != 'v1' or not signature:
            return None
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        user = claims['sub']
        if (claims.get('ver') != 1 or claims.get('scope') != 'agent'
                or not isinstance(claims.get('sid'), str) or not claims['sid']
                or type(user) is not int or not 0 < user <= 9223372036854775807
                or type(claims.get('exp')) is not int or claims['exp'] <= time.time()):
            return None
        return str(user)
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None
