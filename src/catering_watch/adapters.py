from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import parse_qs, quote, urlsplit

from .ingest import replay_fields, timestamp

ACTORS = {
    "douyin": "socialdatax/socialdatax-douyin-data-api",
    "channels": "zen-studio/wechat-channels-scraper",
    "xiaohongshu": "socialdatax/socialdatax-xhs-data-api",
}


def channels_account_ids(row):
    return {k: str(row[k]) for k in ("channelId", "finderUserName", "username") if row.get(k)}


@dataclass
class SearchResult:
    platform: str
    post_id: str
    url: str | None
    published_at: int | None
    title: str | None
    body: str | None
    cover_url: str | None
    author_name: str | None
    author_id: str | None
    author_url: str | None
    duration_seconds: float | None
    raw: dict


class PlatformAdapter:
    platform = ""
    actor = ""
    price_per_item = 0.005
    start_fee = 0.00005

    def prepare_row(self, row):
        return row

    def actor_input(self, payload):
        return payload

    def item_price(self, operation):
        return Decimal(str(self.price_per_item))

    def search_payload(self, query: str, limit: int) -> dict:
        raise NotImplementedError


class DouyinAdapter(PlatformAdapter):
    platform = "douyin"
    actor = ACTORS[platform]
    price_per_item = 0.00499

    def search_payload(self, q, n):
        return {
            "operation": "search_videos",
            "keyword": q,
            "sort_type": "time_descending",
            "publish_time_range": "half_year",
            "content_type": "video",
            "max_items": n,
            "auto_paginate": False,
        }

    def reference_payload(self, reference):
        match = re.search(r"(?:video/|modal_id=)(\d{10,})", reference) or re.fullmatch(
            r"(\d{10,})", reference
        )
        pid = match.group(1) if match else None
        payload = {"operation": "get_video_detail", "max_items": 1, "auto_paginate": False}
        payload["aweme_id" if pid else "url"] = pid or reference
        return payload

    def profile_payload(self, reference, limit):
        return {
            "operation": "list_user_videos",
            "profile_url": reference,
            "max_items": limit,
            "auto_paginate": False,
        }

    def normalize(self, r):
        pid = r.get("aweme_id")
        if not pid:
            return None
        ms = r.get("video_duration_ms")
        return SearchResult(
            self.platform,
            str(pid),
            r.get("share_url") or f"https://www.douyin.com/video/{pid}",
            timestamp(r.get("publish_time")),
            r.get("description"),
            r.get("description"),
            r.get("cover_image_url"),
            r.get("author_name"),
            r.get("author_sec_user_id") or r.get("author_user_id"),
            r.get("author_profile_url"),
            float(ms) / 1000 if ms else None,
            r,
        )


class ChannelsAdapter(PlatformAdapter):
    platform = "channels"
    actor = ACTORS[platform]
    price_per_item = 0.00399
    start_fee = 0.05

    def item_price(self, operation):
        # Free-tier event plus the default dataset-item charge.
        return {"channelSearch": Decimal(".00900"), "videoDetail": Decimal(".02500")}.get(
            operation, Decimal(".00400")
        )

    def actor_input(self, payload):
        # The result limit affects the charge cap and cache key, not the Actor API.
        return {k: v for k, v in payload.items() if k != "_limit"}

    def search_payload(self, q, n, *, sort="latest", published="halfYear"):
        return {
            "operation": "videoSearch",
            "keywords": [q],
            "_limit": n,
            "maxPages": 1,
            "sortBy": sort,
            "publishTime": published,
            "duration": "all",
        }

    def reference_payload(self, reference):
        parts = urlsplit(reference)
        if (
            parts.hostname == "channels.weixin.qq.com"
            and parts.path.rstrip("/") == "/finder-preview/pages/feed"
        ):
            reference = parse_qs(parts.query).get("eid", [reference])[0]
        return {"operation": "videoDetail", "videos": [reference]}

    def profile_payload(self, reference, limit):
        return {
            "operation": "channelVideos",
            "channels": [reference],
            "maxPages": 1,
            "continuePreviousRun": False,
        }

    def normalize(self, r):
        if isinstance(r.get("media"), dict):
            r = replay_fields(r)
        pid = next(
            (
                f"{key}:{r[key]}"
                for key in ("docID", "videoId", "hashDocID", "exportId")
                if r.get(key)
            ),
            None,
        )
        if not pid:
            return None
        source = r["source"] if isinstance(r.get("source"), dict) else {}
        published = r.get("publishedAt") or r.get("pubTime") or r.get("createTime")
        try:
            parts = str(r.get("duration") or r.get("durationText") or "").split(":")
            duration = sum(float(x) * 60**i for i, x in enumerate(reversed(parts)))
        except (ValueError, TypeError):
            duration = None
        title = r.get("title") or r.get("description")
        return SearchResult(
            self.platform,
            str(pid),
            r.get("shareUrl")
            or r.get("url")
            or (
                "https://channels.weixin.qq.com/finder-preview/pages/feed?eid="
                + quote(str(r["exportId"]), safe="/")
                if r.get("exportId")
                else None
            ),
            timestamp(published),
            title,
            title,
            r.get("image"),
            r.get("creatorName") or r.get("nickname") or source.get("title"),
            next(iter(channels_account_ids(r).values()), None),
            r.get("creatorUrl"),
            duration,
            r,
        )


class XiaohongshuAdapter(PlatformAdapter):
    platform = "xiaohongshu"
    actor = ACTORS[platform]
    price_per_item = 0.00499

    def search_payload(self, q, n):
        return {
            "operation": "search_notes",
            "keyword": q,
            "sort_type": "time_descending",
            "note_type": "video",
            "publish_time_range": "half_year",
            "max_items": n,
            "auto_paginate": False,
        }

    def reference_payload(self, reference):
        return {
            "operation": "get_note_detail",
            "note_url": reference,
            "max_items": 1,
            "auto_paginate": False,
        }

    def profile_payload(self, reference, limit):
        return {
            "operation": "list_user_notes",
            "profile_url": reference,
            "max_items": limit,
            "auto_paginate": False,
        }

    def normalize(self, r):
        pid = r.get("note_id")
        if not pid:
            return None
        return SearchResult(
            self.platform,
            str(pid),
            r.get("note_url"),
            timestamp(r.get("publish_time")),
            r.get("title"),
            r.get("summary") or r.get("content"),
            r.get("cover_image_url"),
            r.get("author_name"),
            r.get("author_user_id"),
            r.get("author_profile_url"),
            float(r["video_duration_ms"]) / 1000 if r.get("video_duration_ms") else None,
            r,
        )


ADAPTERS = {"douyin": DouyinAdapter, "channels": ChannelsAdapter, "xiaohongshu": XiaohongshuAdapter}


def is_profile_reference(platform: str, reference: str) -> bool:
    v = reference.lower()
    if platform == "douyin":
        return "/user/" in v
    if platform == "xiaohongshu":
        return "/user/profile/" in v
    parts = urlsplit(reference)
    return parts.hostname == "channels.weixin.qq.com" and (
        "profile" in parts.path.lower().split("/")
        or parts.path.rstrip("/").lower() == "/finder"
        and bool(channels_account_ids(parse_qs(parts.query)))
    )
