"""Recoverable paid requests. Each page is durable before requesting the next."""

import hashlib
import json
import os
import time
from uuid import uuid4
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from psycopg.types.json import Jsonb

from .config import settings
from .db import connect
from .apify import ApifyClient, ApifyError

ACTIVE = ("STARTING", "RUNNING", "RECONCILE")
SUCCESS = ("SUCCEEDED", "SUCCEEDED_WITH_WARNINGS")
PROJECT = settings.project_key
PAGE = 100


def timestamp(value):
    if value is None:
        return None
    try:
        number = float(value)
        seconds = number / 1000 if abs(number) >= 10**11 else number
        return datetime.fromtimestamp(seconds, timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return dt.timestamp() if dt.tzinfo else None
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
    if isinstance(value, str) and value.startswith(("https://", "http://")):
        u = urlsplit(value)
        keep = [(k, v) for k, v in parse_qsl(u.query) if k in {"modal_id", "id", "note_id"}]
        return urlunsplit((u.scheme, u.hostname or "", u.path, urlencode(keep), ""))
    return value


REPLAY_FIELDS = set(
    """
 aweme_id description publish_time video_duration_ms cover_image_url share_url author_name author_sec_user_id
 author_user_id author_profile_url verification_text author_custom_verify author_enterprise_verify_reason
 enterprise_verify_reason docID hashDocID exportId channelId finderUserName username creatorName source
 title image url creatorUrl publishedAt pubTime duration note_id note_url note_type type summary content
 user_id sec_user_id nickname profile_url author_douyin_id author_red_id page_next_page_token page_has_more
 collection_run_id like_count digg_count comment_count share_count collect_count liked_count collected_count
 likes comments shares favorites topics hashtags is_top is_pinned is_ad verification_type
""".split()
)


def replay_fields(row):
    result = {k: clean(v) for k, v in row.items() if k in REPLAY_FIELDS}
    if isinstance(result.get("source"), dict):
        result["source"] = {
            k: v for k, v in result["source"].items() if k in {"title", "id", "username"}
        }
    return result


def key_for(actor, payload, build):
    return hashlib.sha256(json.dumps([actor, payload, build], sort_keys=True).encode()).hexdigest()


def claim(platform, actor, payload, build, cap, floor):
    fp = key_for(actor, payload, build)
    with connect() as db:
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (PROJECT,))
        previous = db.execute(
            """SELECT * FROM catering_watch.fetch_runs WHERE project_key=%s
            AND input_fingerprint=%s AND (status=ANY(%s) OR
            (status=ANY(%s) AND finished_at>now()-%s*interval '1 hour'))
            ORDER BY id DESC LIMIT 1""",
            (PROJECT, fp, list(ACTIVE), list(SUCCESS), settings.cache_ttl_hours),
        ).fetchone()
        if previous:
            return previous, False
        held = db.execute(
            """SELECT coalesce(sum(reserved_usd),0) AS amount FROM catering_watch.fetch_runs
            WHERE project_key=%s AND (started_at >= (date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
            OR status=ANY(%s))""",
            (PROJECT, list(ACTIVE)),
        ).fetchone()["amount"]
        if not settings.apify_token or "<" in settings.apify_token:
            raise ApifyError("APIFY_TOKEN 未配置")
        limit = Decimal(os.getenv("DAILY_BUDGET_USD", str(settings.max_cost_usd)))
        if cap < floor or held + cap > limit:
            raise ApifyError(
                f"预算不足，已跳过 {platform}/{payload['operation']} (minimum {floor}, cap {cap})"
            )
        row = db.execute(
            """INSERT INTO catering_watch.fetch_runs(project_key,platform,operation,input_fingerprint,
            status,idempotency_key,reserved_usd,metadata) VALUES(%s,%s,%s,%s,'STARTING',
            %s,%s,%s) RETURNING *""",
            (
                PROJECT,
                platform,
                payload["operation"],
                fp,
                uuid4().hex,
                cap,
                Jsonb(
                    {
                        "actor": actor,
                        "build": build,
                        "query": payload.get("keyword")
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

    def run(self, actor, payload, max_cost_usd):
        build = os.getenv("APIFY_BUILD", "latest")
        cap = Decimal(str(max_cost_usd)).quantize(Decimal(".0001"), rounding=ROUND_DOWN)
        price = Decimal(str(self.adapter.price_per_item))
        if payload["operation"] == "channelSearch":
            price = Decimal(".00899")
        start = Decimal(str(self.adapter.start_fee))
        row, new = claim(self.adapter.platform, actor, payload, build, cap, start + price)
        rid = row["id"]
        if not new and row["status"] in SUCCESS:
            return self.result(rid)
        if not new and not row["provider_run_id"]:
            raise ApifyError(f"任务 {rid} 正在启动或待核对，请勿重新提交")
        client = self.transport
        if client is None:
            # Missing credentials are checked before claiming new work by the CLI.
            client = ApifyClient()
        if new:
            try:
                run = client.start(actor, payload, cap, build)
                provider = run["id"]
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
        ok = run["status"] in SUCCESS
        live_rows = []
        while True:
            with connect() as db:
                state = db.execute(
                    "SELECT status,result_count FROM catering_watch.fetch_runs WHERE id=%s FOR UPDATE",
                    (rid,),
                ).fetchone()
                if state["status"] != "RUNNING":
                    break
                offset = state["result_count"]
                rows = client.page(run["defaultDatasetId"], offset, PAGE) if ok else []
                if rows:
                    live_rows.extend(rows)
                    db.execute(
                        "INSERT INTO catering_watch.dataset_pages VALUES(%s,%s,%s)",
                        (rid, offset, Jsonb([replay_fields(item) for item in rows])),
                    )
                    offset += len(rows)
                    db.execute(
                        "UPDATE catering_watch.fetch_runs SET result_count=%s WHERE id=%s",
                        (offset, rid),
                    )
                if len(rows) == PAGE:
                    continue
                amount = min(row["reserved_usd"], start + offset * price)
                db.execute(
                    """UPDATE catering_watch.fetch_runs SET status=%s,finished_at=now(),
                    metadata=metadata||%s WHERE id=%s""",
                    (
                        run["status"],
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
                    operation,units,unit_price_usd,amount_usd,estimated) VALUES(%s,%s,'apify',%s,%s,%s,%s,%s,true)""",
                    (PROJECT, rid, actor, payload["operation"], offset, price, amount),
                )
                break
        if not ok:
            raise ApifyError(f"Actor {provider}: {run['status']}")
        meta, stored = self.result(rid)
        return meta, (live_rows if len(live_rows) == len(stored) else stored)

    @staticmethod
    def result(rid):
        with connect() as db:
            row = db.execute(
                "SELECT provider_run_id,status,metadata FROM catering_watch.fetch_runs WHERE id=%s",
                (rid,),
            ).fetchone()
            pages = db.execute(
                "SELECT items FROM catering_watch.dataset_pages WHERE fetch_id=%s ORDER BY page_offset",
                (rid,),
            )
            return {"id": row["provider_run_id"], "status": row["status"], "fetch_id": rid}, [
                item for page in pages for item in page["items"]
            ]
