"""Account discovery and incremental catalogue sync using bounded Actor operations."""

from copy import deepcopy
from decimal import Decimal, ROUND_CEILING
from urllib.parse import urljoin, urlsplit
import os
import httpx
from .adapters import ADAPTERS
from .config import settings
from .db import connect
from .ingest import key_for, SUCCESS, ACTIVE, clean

HOSTS = {
    "douyin": ("douyin.com", "iesdouyin.com"),
    "xiaohongshu": ("xiaohongshu.com", "xhslink.com"),
    "channels": ("weixin.qq.com", "finder.video.qq.com"),
}


def platform_for(url):
    u = urlsplit(url)
    if (
        u.scheme not in {"http", "https"}
        or not u.hostname
        or u.username
        or u.port not in (None, 80, 443)
    ):
        raise ValueError("请输入有效的平台公开链接")
    for p, hosts in HOSTS.items():
        if any(u.hostname == h or u.hostname.endswith("." + h) for h in hosts):
            return p
    raise ValueError("未知平台域名")


def resolve(url, platform):
    if platform_for(url) != platform:
        raise ValueError("平台与链接不符")
    if urlsplit(url).hostname not in {"v.douyin.com", "xhslink.com", "www.xhslink.com"}:
        return url
    with httpx.Client(timeout=8, follow_redirects=False) as client:
        for _ in range(5):
            with client.stream("GET", url) as response:
                if not response.is_redirect:
                    response.raise_for_status()
                    return url
                url = urljoin(url, response.headers["location"])
                if platform_for(url) != platform:
                    raise ValueError("分享链接跳转到了其他平台")
    raise ValueError("分享链接跳转次数过多")


def register(brand, platform, uid, evidence=None):
    if evidence and not evidence.startswith(("https://", "http://")):
        raise ValueError("自营证据需要公开链接")
    with connect() as db:
        db.execute(
            """INSERT INTO catering_watch.tracked_accounts(project_key,brand,platform,stable_id,evidence_url)
            VALUES(%s,%s,%s,%s,%s) ON CONFLICT(project_key,brand,platform,stable_id) DO UPDATE
            SET evidence_url=coalesce(EXCLUDED.evidence_url,catering_watch.tracked_accounts.evidence_url)""",
            (settings.project_key, brand, platform, str(uid), clean(evidence)),
        )


def catalog_for(db, base):
    brands = {b["name"]: deepcopy(b) for b in base}
    for r in db.execute(
        "SELECT * FROM catering_watch.tracked_accounts WHERE project_key=%s ORDER BY brand,platform,stable_id",
        (settings.project_key,),
    ):
        b = brands.setdefault(r["brand"], {"name": r["brand"], "aliases": []})
        b.setdefault("accounts", []).append(
            {"platform": r["platform"], "id": r["stable_id"], "evidence_url": r["evidence_url"]}
        )
    return list(brands.values())


class Budget:
    def __init__(self, cap, execute):
        self.remaining = Decimal(str(cap if cap is not None else settings.max_cost_usd))
        self.execute = execute
        self.steps = []

    def request(self, platform, payload, limit):
        adapter = ADAPTERS[platform]()
        unit = (
            Decimal(".00899")
            if payload["operation"] == "channelSearch"
            else Decimal(str(adapter.price_per_item))
        )
        cost = (Decimal(str(adapter.start_fee)) + unit * limit).quantize(
            Decimal(".0001"), rounding=ROUND_CEILING
        )
        fp = key_for(adapter.actor, payload, os.getenv("APIFY_BUILD", "latest"))
        status = None
        if settings.database_url and "<" not in settings.database_url:
            with connect() as db:
                row = db.execute(
                    """SELECT status FROM catering_watch.fetch_runs WHERE project_key=%s
                  AND input_fingerprint=%s AND (status=ANY(%s) OR (status=ANY(%s) AND finished_at>
                  now()-%s*interval '1 hour')) ORDER BY id DESC LIMIT 1""",
                    (
                        settings.project_key,
                        fp,
                        list(ACTIVE),
                        list(SUCCESS),
                        settings.cache_ttl_hours,
                    ),
                ).fetchone()
                status = row["status"] if row else None
        available = self.remaining
        cap = min(cost, available)
        skipped = not status and cap < Decimal(str(adapter.start_fee)) + unit
        step = {
            "platform": platform,
            "operation": payload["operation"],
            "cap_usd": str(cap),
            "cached_or_active": status,
            "skipped": skipped,
        }
        self.steps.append(step)
        if skipped:
            return None, []
        # A preview cache hit may expire before claim: never allow unreserved spending.
        charge = 0 if status else cap
        self.remaining -= charge
        if not self.execute:
            return None, []
        return adapter.client.run(adapter.actor, payload, charge)


def discover(brand, source, terms, budget, limit):
    peers = []
    found = set()
    for term in dict.fromkeys(terms):
        for platform in ADAPTERS:
            if platform == source or platform in found:
                continue
            hits = search_accounts(platform, term, budget, limit, brand)
            peers.extend(hits)
            if hits:
                found.add(platform)
    return list(dict.fromkeys(peers))


def search_accounts(platform, term, budget, limit, brand=None):
    payload = (
        {"operation": "channelSearch", "keywords": [term], "maxPages": 1}
        if platform == "channels"
        else {"operation": "search_users", "keyword": term, "max_items": limit}
    )
    run, rows = budget.request(platform, payload, limit)
    peers = []
    for row in rows[:limit]:
        fields = {
            "douyin": ("sec_user_id", "author_sec_user_id"),
            "xiaohongshu": ("user_id", "author_user_id"),
            "channels": ("finderUserName", "channelId", "username"),
        }[platform]
        uid = next((row.get(k) for k in fields if row.get(k)), None)
        if uid:
            peers.append((platform, str(uid)))
            if brand:
                register(brand, platform, uid)
    if run:
        from psycopg.types.json import Jsonb

        with connect() as db:
            db.execute(
                "UPDATE catering_watch.fetch_runs SET metadata=metadata||%s WHERE id=%s",
                (
                    Jsonb(
                        {
                            "query": term,
                            "outcome": "candidate" if peers else "sparse" if rows else "empty",
                            "run_id": run["id"],
                        }
                    ),
                    run["fetch_id"],
                ),
            )
    return peers


def sync(brand, platform, uid, budget, limit, persist):
    cursor = None
    cutoff = None
    if budget.execute:
        register(brand, platform, uid)
    if budget.execute or (settings.database_url and "<" not in settings.database_url):
        with connect() as db:
            state = db.execute(
                """SELECT next_cursor,extract(epoch from now()-interval '6 months') AS cutoff
                FROM catering_watch.tracked_accounts WHERE project_key=%s AND brand=%s AND platform=%s
                AND stable_id=%s""",
                (settings.project_key, brand, platform, uid),
            ).fetchone()
            if state:
                cursor, cutoff = state["next_cursor"], state["cutoff"]
    payload = {
        "douyin": {"operation": "list_user_videos", "sec_user_id": uid},
        "channels": {
            "operation": "channelVideos",
            "channels": [uid],
            "maxPages": 1,
            "continuePreviousRun": False,
        },
        "xiaohongshu": {"operation": "list_user_notes", "user_id": uid},
    }[platform]
    if platform != "channels":
        payload.update(max_items=limit, auto_paginate=False)
        if cursor:
            payload["page_token"] = cursor
    run, rows = budget.request(platform, payload, limit)
    if not run:
        return []
    adapter = ADAPTERS[platform]()
    ids = []
    times = []
    for row in rows[:limit]:
        item = adapter.normalize(row)
        if item:
            times.append(item.published_at)
            if item.published_at and item.published_at < cutoff:
                continue
            item.raw["collection_run_id"] = run["id"]
            ids.append(persist(item))
    next_cursor = next(
        (r.get("page_next_page_token") for r in reversed(rows) if r.get("page_has_more")), None
    )
    if times and all(t is not None and t < cutoff for t in times):
        next_cursor = None
    with connect() as db:
        db.execute(
            """UPDATE catering_watch.tracked_accounts SET next_cursor=%s,synced_at=now()
            WHERE project_key=%s AND brand=%s AND platform=%s AND stable_id=%s
            AND next_cursor IS NOT DISTINCT FROM %s""",
            (next_cursor, settings.project_key, brand, platform, uid, cursor),
        )
    return ids
