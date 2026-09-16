"""Douyin discovery adapter.

The reference project uses the same itfaba endpoints, but credentials are
deliberately read only from Agent environment variables here. No API key or
browser cookie is stored in source, Cloud payloads, or Web code.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Callable
from urllib import parse, request


Transport = Callable[[str, Mapping[str, str], Mapping[str, str]], Mapping[str, Any]]


class DouyinApiError(RuntimeError):
    """The channel rejected or could not parse a discovery request."""


class DouyinAdapter:
    def __init__(self, *, api_base: str | None = None, api_key: str | None = None, cookie: str | None = None, transport: Transport | None = None) -> None:
        self.api_base = (api_base or os.getenv("WT_MEDIA_DOUYIN_API_BASE", "https://api.itfaba.com")).rstrip("/")
        self.api_key = api_key or os.getenv("WT_MEDIA_DOUYIN_API_KEY", "")
        self.cookie = cookie if cookie is not None else os.getenv("WT_MEDIA_DOUYIN_COOKIE", "")
        self._transport = transport or self._http_transport

    def fetch_by_url(self, url: str) -> dict[str, Any] | None:
        value = url.strip()
        if not value:
            raise DouyinApiError("source URL is required")
        body = {"id" if value.isdigit() else "shorturl": value}
        response = self._request("/dyVideo/detail", body)
        data = response.get("data")
        if not isinstance(data, Mapping) or not data:
            return None
        return self._normalize(data)

    def search(self, keyword: str, *, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        if not keyword.strip():
            raise DouyinApiError("keyword is required")
        response = self._request("/dyRank", {"keywords": keyword.strip(), "limit": str(min(max(limit, 1), 30)), "offset": str(max(offset, 0)), "sort_type": "0", "content_type": "1", "publish_time": "0", "filter_duration": "0"})
        data = response.get("data") if isinstance(response.get("data"), Mapping) else {}
        raw_items = data.get("data", []) if isinstance(data, Mapping) else []
        return [item for raw in raw_items if isinstance(raw, Mapping) and (item := self._normalize(raw.get("aweme_info", raw))) is not None]

    def author_posts(self, author: str, *, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        if not author.strip():
            raise DouyinApiError("author is required")
        endpoint = os.getenv("WT_MEDIA_DOUYIN_AUTHOR_ENDPOINT", "/dyUser/detail")
        response = self._request(endpoint, {"uid": author.strip(), "nickname": author.strip(), "limit": str(min(max(limit, 1), 30)), "offset": str(max(offset, 0))})
        data = response.get("data") if isinstance(response.get("data"), Mapping) else {}
        candidates = data.get("data", data.get("aweme_list", [])) if isinstance(data, Mapping) else []
        if isinstance(data, Mapping) and data.get("aweme_detail"):
            candidates = [data["aweme_detail"]]
        return [item for raw in candidates if isinstance(raw, Mapping) and (item := self._normalize(raw.get("aweme_info", raw))) is not None]

    def _request(self, path: str, fields: Mapping[str, str]) -> Mapping[str, Any]:
        if not self.api_key:
            raise DouyinApiError("Douyin API credentials are not configured on Agent")
        response = self._transport(path, {str(k): str(v) for k, v in fields.items()}, {"apiKey": self.api_key})
        if response.get("result") != 1:
            raise DouyinApiError(str(response.get("info") or "Douyin API rejected request"))
        return response

    def _http_transport(self, path: str, fields: Mapping[str, str], query: Mapping[str, str]) -> Mapping[str, Any]:
        encoded = parse.urlencode(fields).encode("utf-8")
        url = f"{self.api_base}{path}?{parse.urlencode(query)}"
        headers = {"content-type": "application/x-www-form-urlencoded", "user-agent": "WT-Media-Agent/1"}
        if self.cookie:
            headers["cookie"] = self.cookie
        try:
            with request.urlopen(request.Request(url, data=encoded, headers=headers, method="POST"), timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # pragma: no cover - exercised with transport in tests
            raise DouyinApiError(f"Douyin request failed: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise DouyinApiError("Douyin response must be an object")
        return payload

    def _normalize(self, aweme: Mapping[str, Any]) -> dict[str, Any] | None:
        content_id = str(aweme.get("aweme_id") or aweme.get("id") or "").strip()
        if not content_id:
            return None
        description = str(aweme.get("desc") or aweme.get("preview_title") or "")
        author = aweme.get("author") if isinstance(aweme.get("author"), Mapping) else {}
        video = aweme.get("video") if isinstance(aweme.get("video"), Mapping) else {}
        cover = video.get("origin_cover") if isinstance(video.get("origin_cover"), Mapping) else video.get("cover", {})
        play = video.get("play_addr") if isinstance(video.get("play_addr"), Mapping) else {}
        cover_url = self._first_url(cover)
        source_url = f"https://www.douyin.com/video/{content_id}"
        created = aweme.get("create_time")
        published_at = ""
        if isinstance(created, (int, float)) and created > 0:
            published_at = datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
        return {"platform_content_id": content_id, "title": description[:500], "description": description, "cover_url": cover_url, "source_url": source_url, "author_id": str(author.get("uid") or ""), "author_name": str(author.get("nickname") or ""), "published_at": published_at, "tags": ",".join(re.findall(r"#([^\s#]+)", description)), "raw": dict(aweme)}

    @staticmethod
    def _first_url(value: Any) -> str:
        if isinstance(value, Mapping) and isinstance(value.get("url_list"), list) and value["url_list"]:
            return str(value["url_list"][0])
        return ""
