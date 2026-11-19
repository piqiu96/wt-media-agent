"""Public publication boundary: fixed local inputs, execution control and receipt."""
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, TypedDict

from wt_media_agent.runtime.constants import PublishMethod


class PublicationInputError(RuntimeError):
    """Publication input or execution control does not permit the next action."""


@dataclass(frozen=True)
class PublishPayload:
    video: Path
    cover: Path
    content: Mapping[str, Any]
    platform_uid: str


class PublishReceipt(TypedDict):
    platform_work_id: str


@dataclass(frozen=True)
class PublishControl:
    stopped: Callable[[], bool]
    on_prepared: Callable[[], None]
    before_submit: Callable[[], None]

    def check_stopped(self) -> None:
        if self.stopped():
            raise PublicationInputError('publication execution stopped')


class Publisher(Protocol):
    def publish(self, payload: PublishPayload, control: PublishControl, *,
                method: PublishMethod = PublishMethod.BROWSER) -> PublishReceipt: ...
