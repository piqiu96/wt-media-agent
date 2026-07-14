"""Secret-safe BitBrowser Local API profile scanning."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Callable
from urllib import error as urlerror
from urllib import request as urlrequest


class BitBrowserError(RuntimeError):
    """Base error for BitBrowser adapter failures."""


class BitBrowserResponseError(BitBrowserError):
    """The Local API was unavailable or returned an invalid response."""


class BitBrowserIdentityError(BitBrowserError):
    """Profile ownership could not be verified safely."""


@dataclass(frozen=True)
class BitProfile:
    """Allow-listed Profile facts safe to leave the local runtime."""

    bit_profile_id: str
    profile_user_id: str
    main_user_id: str
    name: str
    seq: int | None
    group_id: str
    group_name: str
    status: int | str | None
    bit_updated_at: str


@dataclass(frozen=True)
class ProfileSnapshot:
    """A full BitBrowser Profile snapshot under one main account tree."""

    main_user_id: str
    profiles: tuple[BitProfile, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "main_user_id": self.main_user_id,
            "profiles": [asdict(profile) for profile in self.profiles],
        }


Transport = Callable[[str, dict[str, object], float], dict[str, object]]


class BitBrowserClient:
    """Reads every Profile through the documented zero-based list API."""

    PAGE_SIZE = 100

    def __init__(
        self,
        base_url: str,
        *,
        transport: Transport | None = None,
        timeout: float = 5.0,
    ) -> None:
        normalized = base_url.strip().rstrip("/")
        if not normalized:
            raise ValueError("BitBrowser Local API URL is required")
        if timeout <= 0:
            raise ValueError("BitBrowser timeout must be positive")
        self._base_url = normalized
        self._transport = transport or _post_json
        self._timeout = timeout

    def scan_profiles(self) -> ProfileSnapshot:
        profiles: list[BitProfile] = []
        page = 0
        while True:
            response = self._transport(
                f"{self._base_url}/browser/list",
                {"page": page, "pageSize": self.PAGE_SIZE},
                self._timeout,
            )
            items = _response_items(response)
            profiles.extend(_safe_profile(item) for item in items)
            if len(items) < self.PAGE_SIZE:
                break
            page += 1

        if not profiles:
            raise BitBrowserIdentityError(
                "BitBrowser identity is unverifiable because no Profiles were returned"
            )
        profile_user_ids = {profile.profile_user_id for profile in profiles}
        if "" in profile_user_ids:
            raise BitBrowserIdentityError(
                "BitBrowser identity is unverifiable because a Profile has no userId"
            )
        main_user_ids = {profile.main_user_id for profile in profiles}
        if "" in main_user_ids:
            raise BitBrowserIdentityError(
                "BitBrowser identity is unverifiable because a Profile has no mainUserId"
            )
        if len(main_user_ids) != 1:
            raise BitBrowserIdentityError(
                "BitBrowser identity is unverifiable because mixed mainUserId values were returned"
            )
        return ProfileSnapshot(main_user_id=next(iter(main_user_ids)), profiles=tuple(profiles))


def _response_items(response: dict[str, object]) -> list[dict[str, object]]:
    if not isinstance(response, dict):
        raise BitBrowserResponseError("BitBrowser response is not an object")
    if response.get("success") is not True:
        message = response.get("msg")
        raise BitBrowserResponseError(
            f"BitBrowser list failed: {message if isinstance(message, str) else 'unknown error'}"
        )
    data = response.get("data")
    if not isinstance(data, dict) or "list" not in data:
        raise BitBrowserResponseError("BitBrowser list response has no data.list")
    items = data["list"]
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise BitBrowserResponseError("BitBrowser data.list is not an object array")
    return items


def _safe_profile(item: dict[str, object]) -> BitProfile:
    bit_profile_id = _string(item.get("id"))
    if not bit_profile_id:
        raise BitBrowserResponseError("BitBrowser Profile has no id")
    seq_value = item.get("seq")
    seq = seq_value if isinstance(seq_value, int) and not isinstance(seq_value, bool) else None
    status = item.get("status")
    if not isinstance(status, (int, str)) or isinstance(status, bool):
        status = None
    return BitProfile(
        bit_profile_id=bit_profile_id,
        profile_user_id=_string(item.get("userId")),
        main_user_id=_string(item.get("mainUserId")),
        name=_string(item.get("name")),
        seq=seq,
        group_id=_string(item.get("groupId")),
        group_name=_string(item.get("groupName")),
        status=status,
        bit_updated_at=_string(item.get("updateTime")),
    )


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _post_json(url: str, payload: dict[str, object], timeout: float) -> dict[str, object]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urlrequest.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(request, timeout=timeout) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except (urlerror.URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BitBrowserResponseError("BitBrowser Local API request failed") from error
    if not isinstance(decoded, dict):
        raise BitBrowserResponseError("BitBrowser response is not an object")
    return decoded
