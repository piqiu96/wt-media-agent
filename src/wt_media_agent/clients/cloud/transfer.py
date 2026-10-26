"""Cloud-Agent 文件传输的 lease、进度与终态。

与其它 Agent 路由不同，这四个入口的含义在 **HTTP 状态** 上：lease 有效时
`heartbeat`/`progress` 返回 200 且没有 body，失效时返回 409。因此
`TransferTransport` 返回 `(status, body)`，body 只在需要读错误码时才有内容。

409 意味着 lease 已不属于本次执行（任务被取消、被重新领取或已终态），
执行器必须停止写入，不能当成普通传输错误重试。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Mapping, Optional

#: `(method, path, payload, headers) -> (http status, decoded body)`. The body is
#: `{}` for the two endpoints that answer `200` with no content.
TransferTransport = Callable[
    [str, str, Mapping[str, object], Mapping[str, str]],
    tuple[int, Mapping[str, object]],
]

#: The keys `LocalLease` requires, in the order the contract lists them.
REQUIRED_LEASE_FIELDS = (
    "task_id",
    "attempt_id",
    "asset_type",
    "asset_id",
    "title",
    "total_bytes",
    "expected_sha256",
    "lease_seconds",
    "download_url",
    "download_url_expires_at",
)

#: The `asset_type` values this Agent is given work for (CHG-20261013-085:
#: a composite output rides the same lease, the same executor and the same
#: naming rules as a material -- only the asset differs).
MATERIAL_ASSET_TYPE = "material"
COMPOSITE_OUTPUT_ASSET_TYPE = "composite_output"
LEASABLE_ASSET_TYPES = (MATERIAL_ASSET_TYPE, COMPOSITE_OUTPUT_ASSET_TYPE)

#: `Completion.status`.
TERMINAL_STATUSES = ("success", "failed", "cancelled")


class TransferError(RuntimeError):
    """A transfer endpoint refused the call."""


class TransferLeaseLostError(TransferError):
    """409：本次执行已不再持有 lease。

    任务被取消、lease 过期后被重新领取、或已经终态，都会到这里；三者都不能
    重试，继续下载只会写一个没人要的文件。
    """


class TransferIntegrityRejectedError(TransferError):
    """`422`: Cloud refused the bytes this executor reported as complete.

    Distinct from a lost lease because the executor did produce something and
    the something was wrong -- it is the one outcome that names a fault in this
    machine's own work rather than in the world around it.
    """


class TransferUnavailableError(TransferError):
    """Any other refusal: a `5xx`, or a status this contract does not define.

    Transient by assumption, so an executor retries it within its attempt budget
    and only then writes the failure down.
    """


@dataclass(frozen=True)
class TransferLease:
    """One leased download, exactly as `LocalLease` defines it.

    Every field but `max_attempts` and `attempt_count` is required by the
    contract, and a lease that omits one is refused rather than defaulted. That
    is not pedantry: a defaulted `download_url` sends the executor to fetch from
    nowhere, and a defaulted `total_bytes` turns the size check that guards the
    finished file into a check that always passes.
    """

    task_id: str
    attempt_id: str
    asset_id: int
    title: str
    total_bytes: int
    expected_sha256: str
    lease_seconds: int
    download_url: str
    download_url_expires_at: str
    #: What the task downloads. The contract requires it and `parse_lease` has
    #: already checked it against `LEASABLE_ASSET_TYPES`, so the default here
    #: only serves the direct constructions in the runner's tests -- a caller
    #: that goes through a claim never sees it defaulted.
    asset_type: str = MATERIAL_ASSET_TYPE
    #: Optional: Cloud sends the game the asset is filed under, or "" when it
    #: has no game and the executor files it under a fixed placeholder.
    game_name: str = ""
    #: Optional: when the asset was published, as a calendar date. Cloud sends
    #: it so the executor can append its `YYYYMMDD` segment to the file name; an
    #: asset without a publish time leaves it None and the segment is omitted
    #: (a composite output never has one).
    published_at: date | None = None
    max_attempts: int = 1
    attempt_count: int = 0


@dataclass(frozen=True)
class TransferTerminal:
    """What Cloud recorded for a finished transfer.

    Returned by `complete` so the executor can confirm the outcome it produced
    against the one that was stored, rather than assuming its own call landed.
    """

    task_id: str
    status: str
    completed_bytes: int
    file_name: Optional[str] = None


def parse_lease(payload: object) -> TransferLease:
    """A `LocalLease` body, or `ValueError` naming what it lacked."""
    if not isinstance(payload, Mapping):
        raise ValueError("a transfer lease must be a JSON object")
    missing = [name for name in REQUIRED_LEASE_FIELDS if payload.get(name) is None]
    if missing:
        raise ValueError(f"the transfer lease is missing {', '.join(missing)}")
    if payload["asset_type"] not in LEASABLE_ASSET_TYPES:
        raise ValueError(
            f"this Agent takes no {payload['asset_type']!r} transfers"
        )
    return TransferLease(
        task_id=str(payload["task_id"]),
        attempt_id=str(payload["attempt_id"]),
        asset_id=int(payload["asset_id"]),
        asset_type=str(payload["asset_type"]),
        title=str(payload["title"]),
        total_bytes=int(payload["total_bytes"]),
        expected_sha256=str(payload["expected_sha256"]),
        lease_seconds=int(payload["lease_seconds"]),
        download_url=str(payload["download_url"]),
        download_url_expires_at=str(payload["download_url_expires_at"]),
        game_name=str(payload.get("game_name") or ""),
        published_at=_parse_publish_date(payload.get("published_at")),
        max_attempts=int(payload.get("max_attempts") or 1),
        attempt_count=int(payload.get("attempt_count") or 0),
    )


def _parse_publish_date(value: object) -> date | None:
    """A lease `published_at` as a date, or None when it is absent or unreadable.

    Naming is cosmetic, so an unparseable value degrades to "no publish time"
    and the segment is omitted rather than refusing the whole lease over a date:
    the cost of being wrong (a download stuck on a lease it cannot read) is
    worse than a file name missing its date segment.
    """
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def parse_terminal(payload: object) -> TransferTerminal:
    """A `TransferTerminal` body, or `ValueError` naming what it lacked."""
    if not isinstance(payload, Mapping):
        raise ValueError("a transfer terminal must be a JSON object")
    missing = [
        name for name in ("task_id", "status", "completed_bytes") if payload.get(name) is None
    ]
    if missing:
        raise ValueError(f"the transfer terminal is missing {', '.join(missing)}")
    file_name = payload.get("file_name")
    return TransferTerminal(
        task_id=str(payload["task_id"]),
        status=str(payload["status"]),
        completed_bytes=int(payload["completed_bytes"]),
        file_name=None if file_name is None else str(file_name),
    )


def completion_body(
    *,
    status: str,
    completed_bytes: Optional[int] = None,
    sha256: Optional[str] = None,
    file_name: Optional[str] = None,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
) -> dict[str, object]:
    """The `Completion` body for `status`, holding to the contract's conditions.

    `Completion` is `additionalProperties: false` with two `allOf` conditions, so
    an unset field must be **absent** rather than null: a null `sha256` on a
    success is a schema violation, not an empty value. The two conditions are
    checked here, where the executor's mistake is still attributable, instead of
    being discovered as a `422` from a server that then cannot say which field it
    disliked.
    """
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"{status!r} is not a terminal transfer status")
    if status == "success" and (completed_bytes is None or sha256 is None):
        raise ValueError("a successful transfer must report its size and its digest")
    if status != "success" and not error_code:
        raise ValueError(f"a {status} transfer must report an error code")
    fields: dict[str, object] = {
        "status": status,
        "completed_bytes": completed_bytes,
        "sha256": sha256,
        "file_name": file_name,
        "error_code": error_code,
        "error_message": error_message,
    }
    return {name: value for name, value in fields.items() if value is not None}
