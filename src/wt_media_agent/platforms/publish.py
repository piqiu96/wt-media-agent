"""Public publication boundary: fixed local inputs, execution control and receipt."""
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, TypedDict

from wt_media_agent.runtime.constants import PublishMethod


class PublicationInputError(RuntimeError):
    """Publication input or execution control does not permit the next action."""


class PublicationTagRejected(PublicationInputError):
    """A matched platform validation rejected one frozen tag; no upstream text."""
    def __init__(self, position: int):
        if type(position) is not int or not 1 <= position <= 10:
            raise ValueError('invalid tag position')
        self.position = position
        super().__init__(f'平台未接受第{position}个标签，本次尚未投稿。请核对标签配置后重新安排发布。')


class ExistingPublicationDraft(PublicationInputError):
    """A platform restored an unsent video that this attempt cannot own."""


@dataclass(frozen=True)
class PublishPayload:
    video: Path
    cover: Path
    content: Mapping[str, Any]
    platform_uid: str
    portrait_cover: Path | None = None
    space_cover: Path | None = None


class PublishReceipt(TypedDict):
    platform_work_id: str


@dataclass(frozen=True)
class PublishControl:
    stopped: Callable[[], bool]
    on_prepared: Callable[[], None]
    before_submit: Callable[[], None]
    # Runner proves the previous same-input attempt safely ended before submit.
    replace_matching_draft: bool = False
    wait_for_human: Callable[[Callable[[], None]], None] | None = None

    def check_stopped(self) -> None:
        if self.stopped():
            raise PublicationInputError('publication execution stopped')


class Publisher(Protocol):
    def publish(self, payload: PublishPayload, control: PublishControl, *,
                method: PublishMethod = PublishMethod.BROWSER) -> PublishReceipt: ...
