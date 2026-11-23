"""Original-submission checking capability; no task, Cloud or browser-page types."""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, ContextManager, Mapping, Protocol

class PublicationCheckError(RuntimeError):
    """Original-submission input or execution control does not permit a read."""


@dataclass(frozen=True)
class PublicationReference:
    platform_uid: str
    submission_id: str
    content: Mapping[str, Any]
    target_url: str = ''


@dataclass(frozen=True)
class PublicWorkObservation:
    conclusion: str
    reason: str
    basis: str
    submission_id: str = ''
    target_url: str = ''
    work_id: str = ''
    work_url: str = ''
    published_at: datetime | None = None
    lookup_result: str = ""


@dataclass(frozen=True)
class PublicationCheckControl:
    """Caller-owned authorization and scoped resources; no Playwright/Cloud types."""
    current: Callable[[], bool]
    browser_fallback_allowed: bool
    read_environment: Callable[[], Mapping[str, Any]]
    browser: Callable[[], ContextManager[str]]


class PublicationChecker(Protocol):
    def check(self, reference: PublicationReference,
              control: PublicationCheckControl) -> PublicWorkObservation: ...
