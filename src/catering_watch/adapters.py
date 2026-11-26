from __future__ import annotations
from dataclasses import dataclass
import re
from .apify import ApifyClient

ACTORS = {
    "douyin": "socialdatax/socialdatax-douyin-data-api",
    "channels": "zen-studio/wechat-channels-scraper",
    "xiaohongshu": "socialdatax/socialdatax-xhs-data-api",
}

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

    def __init__(self, client=None):
        self.client = client or ApifyClient()

    def search(self, query: str, limit: int, max_cost_usd: float):
        run, rows = self.client.run(self.actor, self.search_payload(query, limit), max_cost_usd)
        run["raw_item_count"] = len(rows)
        return run, [item for row in rows if (item := self.normalize(row))]

    def fetch_reference(self, reference: str, max_cost_usd: float):
        raise NotImplementedError

    def fetch_profile(self, reference: str, limit: int, max_cost_usd: float):
        raise NotImplementedError

    def search_payload(self, query: str, limit: int) -> dict:
        raise NotImplementedError

class DouyinAdapter(PlatformAdapter):
    platform = "douyin"
    actor = ACTORS[platform]
    price_per_item = 0.00499

    def search_payload(self, q, n):
        return {"operation":"search_videos","keyword":q,"sort_type":"time_descending",
                "publish_time_range":"half_year","content_type":"video","max_items":n,"auto_paginate":False}

    def fetch_reference(self, reference, max_cost_usd):
        match = re.search(r"(?:video/|modal_id=)(\d{10,})", reference) or re.fullmatch(r"(\d{10,})", reference)
        pid = match.group(1) if match else None
        payload = {"operation":"get_video_detail","max_items":1,"auto_paginate":False}
        payload["aweme_id" if pid else "url"] = pid or reference
        run, rows = self.client.run(self.actor, payload, max_cost_usd)
        return run, next((item for row in rows if (item := self.normalize(row))), None)

    def fetch_profile(self, reference, limit, max_cost_usd):
        run, rows = self.client.run(self.actor, {"operation":"list_user_videos","profile_url":reference,
            "max_items":limit,"auto_paginate":False}, max_cost_usd)
        return run, [item for row in rows if (item := self.normalize(row))][:limit]

    def normalize(self, r):
        pid = r.get("aweme_id")
        if not pid: return None
        ms = r.get("video_duration_ms")
        return SearchResult(self.platform,str(pid),r.get("share_url") or f"https://www.douyin.com/video/{pid}",
            r.get("publish_time"),r.get("description"),r.get("description"),r.get("cover_image_url"),
            r.get("author_name"),r.get("author_sec_user_id") or r.get("author_user_id"),
            r.get("author_profile_url"),float(ms)/1000 if ms else None,r)

class ChannelsAdapter(PlatformAdapter):
    platform = "channels"
    actor = ACTORS[platform]
    price_per_item = 0.00399
    start_fee = 0.05

    def search_payload(self, q, n):
        return {"operation":"videoSearch","keywords":[q],"maxPages":1,"sortBy":"latest",
                "publishTime":"halfYear","duration":"all"}

    def fetch_reference(self, reference, max_cost_usd):
        run, rows = self.client.run(self.actor, {"operation":"videoDetail","videos":[reference]}, max_cost_usd)
        return run, next((item for row in rows if (item := self.normalize(row))), None)

    def fetch_profile(self, reference, limit, max_cost_usd):
        run, rows = self.client.run(self.actor, {"operation":"channelVideos","channels":[reference],
            "maxPages":1,"continuePreviousRun":True}, max_cost_usd)
        return run, [item for row in rows if (item := self.normalize(row))][:limit]

    def normalize(self, r):
        pid = r.get("docID") or r.get("hashDocID") or r.get("exportId")
        if not pid: return None
        source = r.get("source") or {}
        published = r.get("publishedAt") or r.get("pubTime")
        try:
            parts = str(r.get("duration") or "").split(":")
            duration = sum(float(x) * 60**i for i,x in enumerate(reversed(parts)))
        except (ValueError, TypeError):
            duration = None
        title = r.get("title")
        return SearchResult(self.platform,str(pid),r.get("url"),int(published) if published else None,
            title,title,r.get("image"),r.get("creatorName") or source.get("title"),
            r.get("channelId") or r.get("finderUserName"),r.get("creatorUrl"),duration,r)

class XiaohongshuAdapter(PlatformAdapter):
    platform = "xiaohongshu"
    actor = ACTORS[platform]
    price_per_item = 0.00499

    def search_payload(self, q, n):
        return {"operation":"search_notes","keyword":q,"sort_type":"time_descending","note_type":"video",
                "publish_time_range":"half_year","max_items":n,"auto_paginate":False}

    def fetch_reference(self, reference, max_cost_usd):
        run, rows = self.client.run(self.actor, {"operation":"get_note_detail","note_url":reference,
            "max_items":1,"auto_paginate":False}, max_cost_usd)
        return run, next((item for row in rows if (item := self.normalize(row))), None)

    def fetch_profile(self, reference, limit, max_cost_usd):
        run, rows = self.client.run(self.actor, {"operation":"list_user_notes","profile_url":reference,
            "max_items":limit,"auto_paginate":False}, max_cost_usd)
        return run, [item for row in rows if (item := self.normalize(row))][:limit]

    def normalize(self, r):
        pid = r.get("note_id")
        if not pid: return None
        return SearchResult(self.platform,str(pid),r.get("note_url"),r.get("publish_time"),r.get("title"),
            r.get("summary") or r.get("content"),r.get("cover_image_url"),r.get("author_name"),
            r.get("author_user_id"),r.get("author_profile_url"),
            float(r["video_duration_ms"])/1000 if r.get("video_duration_ms") else None,r)

ADAPTERS = {"douyin":DouyinAdapter,"channels":ChannelsAdapter,"xiaohongshu":XiaohongshuAdapter}

def detect_platform(reference: str) -> str:
    value = reference.lower()
    if "douyin.com" in value or "iesdouyin.com" in value: return "douyin"
    if "xiaohongshu.com" in value or "xhslink.com" in value: return "xiaohongshu"
    if any(x in value for x in ("weixin.qq.com/sph","channels.weixin.qq.com","finder.video.qq.com")): return "channels"
    raise ValueError("无法识别平台，请显式提供 platform 或有效作品/主页链接")

def is_profile_reference(platform: str, reference: str) -> bool:
    v = reference.lower()
    if platform == "douyin": return "/user/" in v
    if platform == "xiaohongshu": return "/user/profile/" in v
    return "channels.weixin.qq.com" in v and ("profile" in v or "finder" in v)
