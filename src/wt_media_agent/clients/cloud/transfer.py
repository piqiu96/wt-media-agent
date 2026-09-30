"""Leases, progress and terminal states for the Cloud-Agent transfer surface.

Four endpoints live under `/api/v1/cloud-agent/file-transfer-tasks/`: `claim`
(which carries no request body at all -- the node is whoever the bearer
credential names), `{task_id}/heartbeat`, `{task_id}/progress`, and
`{task_id}/complete`. They are the only part of the Cloud-Agent API whose
meaning is carried by the **HTTP status** rather than by the response envelope,
and that one fact shapes this module.

`heartbeat` and `progress` answer `200` with no body whatsoever while the lease
stands, and `409` once it does not. There is no `data` object to inspect and no
errcode to branch on, so a client built like the rest of this package -- decode
a body, look for `data` -- has nothing to read. It has to read the status.
`TransferTransport` therefore returns `(status, body)` instead of the body
alone, and the four methods that use it are the only ones that do. The older
surface keeps its own transport: `report_task` and its neighbours answer with a
body in every case that matters to them, and widening their contract would
change behaviour that already works.

`409` is the fact the download executor cannot do without. It means the lease is
gone -- the task was cancelled, or taken over, or has already reached a terminal
state -- and an executor that did not see it would keep writing bytes for a task
nobody is listening to. It is not an edge case to be swallowed into a generic
transport error.

The node credential is the identity on these calls, so it is exactly what a
careless message would quote. Nothing in this module renders it: it is passed to
the transport as a header and appears in no message this module composes.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    "asset_type",
    "asset_id",
    "title",
    "total_bytes",
    "expected_sha256",
    "lease_seconds",
    "download_url",
    "download_url_expires_at",
)

#: The only `asset_type` this Agent is given work for.
MATERIAL_ASSET_TYPE = "material"

#: `Completion.status`.
TERMINAL_STATUSES = ("success", "failed", "cancelled")


class TransferError(RuntimeError):
    """A transfer endpoint refused the call."""


class TransferLeaseLostError(TransferError):
    """`409`: this node no longer holds the lease.

    The task may have been cancelled by its owner, taken over after the lease
    expired, or already finished. All three end the download, and none of them
    is the executor's to retry, so they share one type. An executor that treated
    this as transient would download a file nobody asked for any more.
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
    asset_id: int
    title: str
    total_bytes: int
    expected_sha256: str
    lease_seconds: int
    download_url: str
    download_url_expires_at: str
    #: Optional: Cloud sends the game a material is filed under, or "" when the
    #: material has no game and the executor files it under a fixed placeholder.
    game_name: str = ""
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
    if payload["asset_type"] != MATERIAL_ASSET_TYPE:
        raise ValueError(
            f"this Agent takes no {payload['asset_type']!r} transfers"
        )
    return TransferLease(
        task_id=str(payload["task_id"]),
        asset_id=int(payload["asset_id"]),
        title=str(payload["title"]),
        total_bytes=int(payload["total_bytes"]),
        expected_sha256=str(payload["expected_sha256"]),
        lease_seconds=int(payload["lease_seconds"]),
        download_url=str(payload["download_url"]),
        download_url_expires_at=str(payload["download_url_expires_at"]),
        game_name=str(payload.get("game_name") or ""),
        max_attempts=int(payload.get("max_attempts") or 1),
        attempt_count=int(payload.get("attempt_count") or 0),
    )


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
