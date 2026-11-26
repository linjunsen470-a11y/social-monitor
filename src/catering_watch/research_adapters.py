"""Map public search Actor responses before projecting the durable dataset cache."""

from .adapters import DouyinAdapter, XiaohongshuAdapter


def as_dict(value):
    return value if isinstance(value, dict) else {}


class PublicSearch:
    def actor_input(self, payload):
        return {k: v for k, v in payload.items() if k != "operation"}

    def search_payload(self, query, limit):
        return {"operation": "public_post_search", self.query_field: query, "maxItems": limit}


class PublicDouyin(PublicSearch, DouyinAdapter):
    actor = "toolzerhub/douyin-video-search-scraper"
    query_field = "keyword"
    price_per_item = 0.0056

    def prepare_row(self, row):
        # Also supports the flat and authorMeta variants of archived batch searches.
        author = as_dict(row.get("author"))
        meta = as_dict(row.get("authorMeta"))
        return {
            **row,
            "aweme_id": row.get("aweme_id") or (row.get("id") if meta else None),
            "description": row.get("description")
            or row.get("desc")
            or row.get("text")
            or row.get("caption"),
            "publish_time": row.get("publish_time")
            or row.get("create_time")
            or row.get("createTime"),
            "author_name": row.get("author_name") or author.get("nickname") or meta.get("name"),
            "author_sec_user_id": row.get("author_sec_user_id")
            or author.get("sec_uid")
            or meta.get("secUid"),
            "author_user_id": row.get("author_user_id") or author.get("uid") or meta.get("id"),
            "verification_text": row.get("verification_text")
            or row.get("author_enterprise_verify_reason")
            or row.get("enterprise_verify_reason")
            or author.get("enterprise_verify_reason")
            or row.get("author_custom_verify")
            or meta.get("verificationReason"),
            "share_url": row.get("share_url") or row.get("shareUrl") or row.get("url"),
            "video_duration_ms": row.get("video_duration_ms")
            or as_dict(row.get("video")).get("duration"),
        }


class PublicRednote(PublicSearch, XiaohongshuAdapter):
    actor = "toolzerhub/rednote-xiaohongshu-search-scraper"
    query_field = "query"
    price_per_item = 0.006

    def prepare_row(self, row):
        if row.get("note_id"):
            return row
        user = as_dict(row.get("user"))
        pid = row.get("id")
        return {
            "note_id": pid,
            "note_url": f"https://www.xiaohongshu.com/explore/{pid}" if pid else None,
            "title": row.get("title") or row.get("desc"),
            "content": row.get("desc"),
            "note_type": row.get("type"),
            "author_name": user.get("nickname"),
            "author_user_id": user.get("userid") or user.get("user_id"),
            "publish_time": row.get("timestamp"),
        }


PEER_SEARCH = {"douyin": PublicDouyin, "xiaohongshu": PublicRednote}
