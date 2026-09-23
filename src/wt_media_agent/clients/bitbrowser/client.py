"""Secret-safe BitBrowser Local API profile scanning."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Callable
from urllib import error as urlerror
from urllib import request as urlrequest

from wt_media_agent.clients.bitbrowser.errors import (
    BitBrowserIdentityError,
    BitBrowserResponseError,
)
from wt_media_agent.clients.bitbrowser.timeouts import (
    DEFAULT_OPERATION_TIMEOUT,
    effective_timeout,
)


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
    remark: str
    proxy_type: str
    proxy_host: str
    proxy_port: int


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
    DEFAULT_CREATE_TIMEOUT = DEFAULT_OPERATION_TIMEOUT
    DEFAULT_MUTATION_TIMEOUT = DEFAULT_OPERATION_TIMEOUT

    def __init__(
        self,
        base_url: str,
        *,
        transport: Transport | None = None,
        timeout: float = 5.0,
        create_timeout_override: float | None = None,
        mutation_timeout_override: float | None = None,
    ) -> None:
        normalized = base_url.strip().rstrip("/")
        if not normalized:
            raise ValueError("BitBrowser Local API URL is required")
        if timeout <= 0:
            raise ValueError("BitBrowser timeout must be positive")
        self._base_url = normalized
        self._transport = transport or _post_json
        self._timeout = timeout
        # Passed in rather than read from the environment: this module is a
        # client, and `runtime/config.py` is the only place that reads env
        # (CHG-056 T-03).
        self._create_timeout_override = create_timeout_override
        self._mutation_timeout_override = mutation_timeout_override

    def _create_timeout(self) -> float:
        return effective_timeout(
            self._timeout, self._create_timeout_override, self.DEFAULT_CREATE_TIMEOUT
        )

    def _mutation_timeout(self) -> float:
        return effective_timeout(
            self._timeout, self._mutation_timeout_override, self.DEFAULT_MUTATION_TIMEOUT
        )

    def _post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        return self._transport(
            f"{self._base_url}{path}",
            payload,
            self._timeout,
        )

    def _post_with_timeout(self, path: str, payload: dict[str, object], timeout: float) -> dict[str, object]:
        return self._transport(
            f"{self._base_url}{path}",
            payload,
            timeout,
        )

    def _check_success(self, response: dict[str, object]) -> dict[str, object]:
        if not isinstance(response, dict):
            raise BitBrowserResponseError("BitBrowser response is not an object")
        if response.get("success") is not True:
            msg = response.get("msg", "unknown error")
            raise BitBrowserResponseError(f"BitBrowser request failed: {msg}")
        data = response.get("data")
        return data if isinstance(data, dict) else {}

    def create_profile(self, config: dict[str, object]) -> str:
        """Create a new browser profile. Returns the new profile ID."""
        payload = _create_profile_payload(config)
        result = self._check_success(self._post_with_timeout("/browser/update", payload, self._create_timeout()))
        profile_id = result.get("id") or result.get("browserId") or result.get("profileId") or ""
        if not profile_id:
            raise BitBrowserResponseError("BitBrowser create profile returned no id")
        return str(profile_id)

    def open_profile(self, profile_id: str) -> None:
        """Open a browser profile in the BitBrowser application."""
        try:
            self._check_success(self._post_with_timeout("/browser/open", {"id": profile_id}, self._mutation_timeout()))
        except BitBrowserResponseError as error:
            message = str(error)
            if "正在打开" in message or "已打开" in message:
                return
            raise

    def open_profile_with_devtools(self, profile_id: str) -> str:
        """Open a profile and return its DevTools http endpoint (host:port).

        Returns '' while the profile is still opening (no endpoint yet); propagates
        real BitBrowser errors so callers can surface them.
        """
        try:
            response = self._post_with_timeout("/browser/open", {"id": profile_id}, self._mutation_timeout())
            data = self._check_success(response)
        except BitBrowserResponseError as error:
            if "正在打开" in str(error) or "已打开" in str(error):
                return ""
            raise
        return str(data.get("http") or "").strip()

    def close_profile(self, profile_id: str) -> None:
        """Close a browser profile."""
        try:
            self._check_success(self._post_with_timeout("/browser/close", {"id": profile_id}, self._mutation_timeout()))
        except BitBrowserResponseError:
            pass  # Close may fail if already closed

    def _read_profile_runtime_fields(self, profile_id: str) -> tuple[dict[str, object], object]:
        """Return (browserFingerPrint, proxyMethod) for a profile from /browser/detail.

        The fingerprint is sensitive browser identity and must not leave the local
        runtime. Restoring Cloud config must pass the existing fingerprint and proxy
        method back unchanged; an empty fingerprint or a missing proxy method would
        let BitBrowser regenerate/reset them. Raise instead of writing when unreadable.

        /browser/list does not expose either field; /browser/detail returns
        data.browserFingerPrint and data.proxyMethod.
        """
        response = self._transport(
            f"{self._base_url}/browser/detail",
            {"id": profile_id},
            self._timeout,
        )
        data = self._check_success(response)
        fingerprint = data.get("browserFingerPrint")
        if not isinstance(fingerprint, dict):
            raise BitBrowserResponseError(
                f"无法读取窗口 {profile_id} 的当前指纹，为保护浏览器身份不执行更新"
            )
        return fingerprint, data.get("proxyMethod")

    def update_profile(self, profile_id: str, config: dict[str, object]) -> None:
        """Update an existing browser profile.

        BitBrowser /browser/update requires browserFingerPrint and proxyMethod. Preserve
        the profile's current values instead of sending empty/absent ones, so the browser
        identity is not regenerated and the proxy method is not reset.
        """
        payload = dict(config)
        payload["id"] = profile_id
        if "browserFingerPrint" not in payload or "proxyMethod" not in payload:
            fingerprint, proxy_method = self._read_profile_runtime_fields(profile_id)
            payload.setdefault("browserFingerPrint", fingerprint)
            if "proxyMethod" not in payload:
                payload["proxyMethod"] = proxy_method if proxy_method is not None else 2
        self._check_success(self._post_with_timeout("/browser/update", payload, self._mutation_timeout()))

    def delete_profile(self, profile_id: str) -> None:
        """Delete a browser profile."""
        self._check_success(self._post("/browser/delete", {"id": profile_id}))

    def group_list(self) -> list[dict[str, object]]:
        """List all profile groups."""
        result = self._check_success(self._post("/group/list", {"page": 0, "pageSize": self.PAGE_SIZE}))
        groups = result.get("list", []) if isinstance(result, dict) else []
        return groups if isinstance(groups, list) else []

    def read_cookies(self, profile_id: str) -> list[dict[str, object]]:
        """Read the profile's stored cookies from /browser/detail.

        This BitBrowser version exposes the profile's active cookies in the
        /browser/detail `cookie` JSON array field; there is no /browser/cookie
        endpoint. Cookie values are sensitive and must not be logged.
        """
        response = self._transport(f"{self._base_url}/browser/detail", {"id": profile_id}, self._timeout)
        data = self._check_success(response)
        raw = data.get("cookie")
        if not isinstance(raw, str) or not raw.strip():
            return []
        try:
            cookies = json.loads(raw)
        except (ValueError, TypeError):
            return []
        return cookies if isinstance(cookies, list) else []

    def save_cookies(self, profile_id: str, cookies: list[dict[str, object]]) -> None:
        """Save cookies to a browser profile."""
        self._check_success(self._post("/browser/cookie/save", {
            "id": profile_id,
            "cookies": cookies,
        }))

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
    proxy_port_raw = item.get("port") or 0
    proxy_port = int(proxy_port_raw) if isinstance(proxy_port_raw, (int, str)) and str(proxy_port_raw).isdigit() else 0
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
        remark=_string(item.get("remark")),
        proxy_type=_string(item.get("proxyType")),
        proxy_host=_string(item.get("host")),
        proxy_port=proxy_port,
    )


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _create_profile_payload(config: dict[str, object]) -> dict[str, object]:
    payload = dict(config)
    payload.setdefault("browserFingerPrint", {})
    if not payload.get("host"):
        payload.setdefault("proxyMethod", 2)
        payload.setdefault("proxyType", "noproxy")
    return payload


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
        raise BitBrowserResponseError(f"BitBrowser Local API request failed: {error}") from error
    if not isinstance(decoded, dict):
        raise BitBrowserResponseError("BitBrowser response is not an object")
    return decoded
