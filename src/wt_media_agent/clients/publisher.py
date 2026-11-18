"""Platform boundary: fixed inputs and evidence, never task authorization."""
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, TypedDict


@dataclass(frozen=True)
class PublishPayload:
    video: Path
    cover: Path
    content: Mapping[str, Any]


class PublishReceipt(TypedDict):
    platform_work_id: str


class Publisher(Protocol):
    def fill_form(self, page, payload: PublishPayload, *, stopped: Callable[[], bool]) -> None: ...
    def submit(self) -> None: ...
    def read_receipt(self) -> PublishReceipt: ...


def publisher_for(platform: str) -> Publisher:
    if platform != 'baidu':
        raise ValueError('platform publisher is not available')
    from wt_media_agent.clients.baidu.publisher import BaiduPublisher
    return BaiduPublisher()
