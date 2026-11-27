"""Recoverable paid requests. Each page is durable before requesting the next."""

import hashlib
import json
import time
from contextlib import nullcontext
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from .apify import ApifyClient, ApifyError, StartRejected
from .config import actor_build, env_value, settings
from .db import connect

ACTIVE = ("STARTING", "RUNNING", "RECONCILE")
PAGE = 100


class InsufficientBudget(ApifyError):
    pass


def timestamp(value):
    if value is None:
        return None
    try:
        number = float(value)
        seconds = number / 1000 if abs(number) >= 10**11 else number
        return datetime.fromtimestamp(seconds, UTC).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        try:
            dt = datetime.fromisoformat(str(value))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            return dt.timestamp()
        except (ValueError, OverflowError):
            return None


def clean(value):
    if isinstance(value, dict):
        return {
            k: clean(v)
            for k, v in value.items()
            if k == "page_next_page_token"
            or not any(
                s in k.lower()
                for s in (
                    "token",
                    "secret",
                    "password",
                    "cookie",
                    "signature",
                    "authorization",
                    "api_key",
                )
            )
        }
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, str) and value.lower().startswith(("https://", "http://")):
        try:
            u = urlsplit(value)
            hostname = u.hostname
            port = u.port
        except ValueError:
            return None
        if not hostname:
            return None
        public_ids = {"modal_id", "id", "note_id"}
        if u.hostname in {"channels.weixin.qq.com", "finder.video.qq.com"}:
            public_ids.update(("channelId", "finderUserName", "username"))
        if (
            u.hostname == "channels.weixin.qq.com"
            and u.path.rstrip("/") == "/finder-preview/pages/feed"
        ):
            public_ids.add("eid")
        keep = [(k, v) for k, v in parse_qsl(u.query) if k in public_ids]
        host = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None:
            host = f"{host}:{port}"
        return urlunsplit((u.scheme, host, u.path, urlencode(keep), ""))
    return value


def cover_url(value):
    if not isinstance(value, str) or not value.startswith(("https://", "http://")):
        return None
    try:
        u = urlsplit(value)
        if not u.hostname or u.username is not None or u.port not in (None, 80, 443):
            return None
    except ValueError:
        return None
    # Re-encoding a signature (even a space or a percent escape) can invalidate it.
    parts = []
    for part in u.query.split("&"):
        key = unquote_plus(part.partition("=")[0]).casefold().replace("-", "_")
        wechat_media_token = key == "token" and u.hostname == "wxapp.tc.qq.com"
        if wechat_media_token or not any(
            s in key
            for s in ("token", "password", "secret", "cookie", "authorization", "api_key", "apikey")
        ):
            parts.append(part)
    return urlunsplit((u.scheme, u.netloc, u.path, "&".join(parts), ""))


REPLAY_FIELDS = {
    "aweme_id", "note_id", "docID", "videoId", "hashDocID", "exportId",
    "channelId", "finderUserName", "username", "user_id", "sec_user_id",
    "author_sec_user_id", "author_user_id", "author_douyin_id", "author_red_id",
    "author_name", "creatorName", "name", "nickname", "channelName", "channelTitle",
    "profile_url", "author_profile_url", "creatorUrl", "note_url", "share_url", "shareUrl", "url",
    "publish_time", "publishedAt", "pubTime", "createTime",
    "title", "description", "desc", "content", "summary", "source", "bio", "introduction",
    "video_duration_ms", "duration", "durationText", "cover_image_url", "image", "note_type", "type",
    "verification_text", "verification_type", "verifiedEntity", "verification_label",
    "author_enterprise_verify_reason", "enterprise_verify_reason", "author_custom_verify",
    "like_count", "digg_count", "comment_count", "share_count", "collect_count", "liked_count",
    "collected_count", "likes", "comments", "shares", "favorites", "topics", "hashtags",
    "follower_count", "fans_count", "fansCount", "is_top", "is_pinned", "is_ad",
    "collection_run_id", "page_next_page_token", "page_has_more", "page_item_count",
}


def replay_fields(row, operation=None):
    result = {
        k: cover_url(v) if k in {"cover_image_url", "image"} else clean(v)
        for k, v in row.items()
        if k in REPLAY_FIELDS
    }
    if isinstance(result.get("source"), dict):
        result["source"] = {
            k: v for k, v in result["source"].items() if k in {"title", "id", "username"}
        }
    if (
        operation == "channelSearch"
        and not result.get("bio")
        and isinstance(row.get("signature"), str)
    ):
        result["bio"] = clean(row["signature"])
    media = row.get("media")
    if isinstance(media, dict) and isinstance(media.get("coverUrl"), str):
        cover = media["coverUrl"]
        suffix = media.get("coverUrlToken")
        if isinstance(suffix, str) and suffix.startswith("&token="):
            cover += suffix if "?" in cover else "?" + suffix[1:]
        result["image"] = result.get("image") or cover_url(cover)
    if isinstance(media, dict) and media.get("videoPlayLen") is not None:
        result["duration"] = result.get("duration") or media["videoPlayLen"]
    return {k: v for k, v in result.items() if v not in (None, "", {}, [])}


def key_for(actor, payload, build):
    return hashlib.sha256(json.dumps([actor, payload, build], sort_keys=True).encode()).hexdigest()


def claim(platform, actor, payload, build, cap, floor):
    fp = key_for(actor, payload, build)
    with connect() as db:
        project = settings.project_key
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (project,))
        previous = db.execute(
            """SELECT * FROM catering_watch.fetch_runs WHERE project_key=%s
            AND input_fingerprint=%s AND (status=ANY(%s) OR
            (status='SUCCEEDED' AND finished_at>now()-%s*interval '1 hour'))
            ORDER BY id DESC LIMIT 1""",
            (project, fp, list(ACTIVE), settings.cache_ttl_hours),
        ).fetchone()
        if previous:
            return previous, False
        held = db.execute(
            """SELECT coalesce(sum(reserved_usd),0) AS amount FROM catering_watch.fetch_runs
            WHERE project_key=%s AND (coalesce(finished_at, started_at) >= (date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
            OR status=ANY(%s))""",
            (project, list(ACTIVE)),
        ).fetchone()["amount"]
        limit = Decimal(env_value("DAILY_BUDGET_USD", str(settings.max_cost_usd)))
        if cap < floor or held + cap > limit:
            raise InsufficientBudget(
                f"预算不足，已跳过 {platform}/{payload['operation']} (minimum {floor}, cap {cap})"
            )
        if not settings.apify_token:
            raise ApifyError("APIFY_TOKEN 未配置")
        row = db.execute(
            """INSERT INTO catering_watch.fetch_runs(project_key,platform,operation,input_fingerprint,
            status,reserved_usd,metadata) VALUES(%s,%s,%s,%s,'STARTING',
            %s,%s) RETURNING *""",
            (
                project,
                platform,
                payload["operation"],
                fp,
                cap,
                Jsonb(
                    {
                        "actor": actor,
                        "build": build,
                        "request_cap_usd": str(cap),
                        "query": payload.get("keyword")
                        or payload.get("query")
                        or next(iter(payload.get("keywords", [])), None),
                    }
                ),
            ),
        ).fetchone()
        return row, True


class DurableClient:
    def __init__(self, adapter, transport=None):
        self.adapter = adapter
        self.transport = transport

    def run(self, actor, payload, max_cost_usd, *, on_cost=None):
        build = actor_build()
        cap = Decimal(str(max_cost_usd)).quantize(Decimal(".0001"), rounding=ROUND_DOWN)
        price = self.adapter.item_price(payload["operation"])
        start = Decimal(str(self.adapter.start_fee))
        row, new = claim(self.adapter.platform, actor, payload, build, cap, start + price)
        rid = row["id"]
        if not new and row["status"] == "SUCCEEDED":
            return self.result(rid, new=False, cached=True)
        if not new and not row["provider_run_id"]:
            raise ApifyError(f"任务 {rid} 正在启动或待核对，请勿重新提交")
        if new and on_cost:
            on_cost(row["reserved_usd"])
        http = nullcontext(self.transport) if self.transport is not None else ApifyClient()
        with http as client:
            try:
                self._collect(client, row, new, actor, payload, build)
            except StartRejected:
                if new and on_cost:
                    on_cost(Decimal(0))
                raise
        result, items = self.result(rid, new=new, cached=False)
        if new and on_cost:
            on_cost(result["command_cost_usd"])
        if result["status"] != "SUCCEEDED":
            raise ApifyError(f"Actor {result['id']}: {result['status']}")
        return result, items

    def _collect(self, client, row, new, actor, payload, build):
        rid = row["id"]
        if new:
            try:
                run = client.start(
                    actor, self.adapter.actor_input(payload), row["reserved_usd"], build
                )
                provider = run["id"]
            except StartRejected as exc:
                with connect() as db:
                    db.execute(
                        """UPDATE catering_watch.fetch_runs SET status='FAILED',reserved_usd=0,
                        finished_at=now(),error_message=%s WHERE id=%s""",
                        (str(exc), rid),
                    )
                raise
            except Exception:
                with connect() as db:
                    db.execute(
                        "UPDATE catering_watch.fetch_runs SET status='RECONCILE' WHERE id=%s",
                        (rid,),
                    )
                raise
            with connect() as db:
                db.execute(
                    "UPDATE catering_watch.fetch_runs SET status='RUNNING',provider_run_id=%s WHERE id=%s",
                    (provider, rid),
                )
        else:
            provider = row["provider_run_id"]
            run = client.status(provider)
        deadline = time.monotonic() + settings.http_timeout_seconds
        while run["status"] in {"READY", "RUNNING", "TIMING-OUT", "ABORTING"}:
            if time.monotonic() >= deadline:
                raise ApifyError(f"任务 {provider} 仍在运行；使用相同参数续跑")
            time.sleep(1)
            run = client.status(provider)
        ok = run["status"] == "SUCCEEDED"
        offset = row["result_count"]
        price = self.adapter.item_price(payload["operation"])
        while True:
            # Only the compare-and-commit phase needs the lock. Another worker may
            # have advanced the cursor while this page was in flight.
            rows = client.page(run["defaultDatasetId"], offset, PAGE) if ok else []
            items = [replay_fields(self.adapter.prepare_row(r), payload["operation"]) for r in rows]
            with connect() as db:
                state = db.execute(
                    "SELECT status,result_count FROM catering_watch.fetch_runs WHERE id=%s FOR UPDATE",
                    (rid,),
                ).fetchone()
                if state["status"] != "RUNNING":
                    break
                if state["result_count"] != offset:
                    offset = state["result_count"]
                    continue
                if items:
                    db.execute(
                        "INSERT INTO catering_watch.dataset_pages(fetch_id,page_offset,items) VALUES(%s,%s,%s)",
                        (rid, offset, Jsonb(items)),
                    )
                    offset += len(rows)
                    db.execute(
                        "UPDATE catering_watch.fetch_runs SET result_count=%s WHERE id=%s",
                        (offset, rid),
                    )
                if len(rows) == PAGE:
                    continue
                reported = run.get("usageTotalUsd")
                estimated = reported is None
                reserved = row["reserved_usd"]
                amount = (
                    min(reserved, Decimal(str(self.adapter.start_fee)) + offset * price)
                    if ok
                    else reserved
                )
                if not estimated:
                    amount = reserved = Decimal(str(reported))
                db.execute(
                    """UPDATE catering_watch.fetch_runs SET status=%s,finished_at=now(),reserved_usd=%s,
                    metadata=metadata||%s WHERE id=%s""",
                    (
                        run["status"],
                        reserved,
                        Jsonb(
                            {
                                "actor_build_id": run.get("buildId"),
                                "dataset_id": run.get("defaultDatasetId"),
                                "usage_total_usd": run.get("usageTotalUsd"),
                            }
                        ),
                        rid,
                    ),
                )
                db.execute(
                    """INSERT INTO catering_watch.cost_ledger(project_key,fetch_run_id,provider,actor_name,
                    operation,units,unit_price_usd,amount_usd,estimated) VALUES(%s,%s,'apify',%s,%s,%s,%s,%s,%s)""",
                    (
                        settings.project_key,
                        rid,
                        actor,
                        payload["operation"],
                        offset,
                        price,
                        amount,
                        estimated,
                    ),
                )
                break

    @staticmethod
    def result(rid, *, new, cached):
        with connect() as db:
            row = db.execute(
                "SELECT provider_run_id,status,reserved_usd FROM catering_watch.fetch_runs WHERE id=%s",
                (rid,),
            ).fetchone()
            pages = db.execute(
                "SELECT items FROM catering_watch.dataset_pages WHERE fetch_id=%s ORDER BY page_offset",
                (rid,),
            )
            return {
                "id": row["provider_run_id"],
                "status": row["status"],
                "fetch_id": rid,
                "command_cost_usd": row["reserved_usd"] if new else Decimal(0),
                "cached": cached,
            }, [item for page in pages for item in page["items"]]
