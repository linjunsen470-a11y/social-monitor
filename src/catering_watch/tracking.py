"""Account discovery and incremental catalogue sync using bounded Actor operations."""

import os
from copy import deepcopy
from decimal import ROUND_CEILING, Decimal
from urllib.parse import urljoin, urlsplit

import httpx
from psycopg.types.json import Jsonb

from .adapters import ADAPTERS, channels_account_ids
from .apify import StartRejected
from .config import settings
from .db import connect, upsert_account
from .ingest import ACTIVE, DurableClient, InsufficientBudget, clean, key_for

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
        """SELECT * FROM catering_watch.tracked_accounts WHERE project_key=%s
        ORDER BY brand,platform,stable_id""",
        (settings.project_key,),
    ):
        b = brands.setdefault(r["brand"], {"name": r["brand"], "aliases": []})
        b.setdefault("accounts", []).append(
            {"platform": r["platform"], "id": r["stable_id"], "evidence_url": r["evidence_url"]}
        )
    # Only IDs observed together on a provider account can share a tracking/ownership link.
    aliases = {}
    canonical = {}
    for row in db.execute(
        """SELECT source_metadata FROM catering_watch.accounts WHERE project_key=%s
        AND platform='channels' AND source_metadata <> '{}'::jsonb""",
        (settings.project_key,),
    ):
        ids = set(channels_account_ids(row["source_metadata"]).values())
        for uid in ids:
            aliases.setdefault(uid, set()).update(ids)
            if row["source_metadata"].get("channelId"):
                canonical[uid] = row["source_metadata"]["channelId"]
    for brand in brands.values():
        for account in brand.get("accounts", []):
            if account["platform"] == "channels":
                account["ids"] = sorted(aliases.get(account["id"], {account["id"]}))
                account["id"] = canonical.get(account["id"], account["id"])
    return list(brands.values())


class Budget:
    def __init__(self, cap, execute):
        self.remaining = Decimal(str(cap if cap is not None else settings.max_cost_usd))
        self.execute = execute
        self.steps = []

    def request(self, platform, payload, limit, *, adapter=None):
        adapter = adapter or ADAPTERS[platform]()
        unit = adapter.item_price(payload["operation"])
        cost = (Decimal(str(adapter.start_fee)) + unit * limit).quantize(
            Decimal(".0001"), rounding=ROUND_CEILING
        )
        status = None
        if not self.execute and settings.database_url and "<" not in settings.database_url:
            fp = key_for(adapter.actor, payload, os.getenv("APIFY_BUILD", "latest"))
            with connect() as db:
                row = db.execute(
                    """SELECT status FROM catering_watch.fetch_runs WHERE project_key=%s
                  AND input_fingerprint=%s AND (status=ANY(%s) OR (status='SUCCEEDED' AND finished_at>
                  now()-%s*interval '1 hour')) ORDER BY id DESC LIMIT 1""",
                    (
                        settings.project_key,
                        fp,
                        list(ACTIVE),
                        settings.cache_ttl_hours,
                    ),
                ).fetchone()
                status = row["status"] if row else None
        cap = min(cost, self.remaining)
        skipped = not self.execute and not status and cap < Decimal(str(adapter.start_fee)) + unit
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
        if not self.execute:
            if not status:
                self.remaining -= cap
            return None, []
        # claim() checks the cache, the reservation and both limits in one transaction.
        try:
            run, rows = DurableClient(adapter).run(adapter.actor, payload, cap)
        except InsufficientBudget:
            step["skipped"] = True
            return None, []
        except StartRejected:
            raise
        except Exception:
            self.remaining -= cap
            raise
        step["cached_or_active"] = run["status"] if run["cached"] else None
        step["charged_or_held_usd"] = str(run["command_cost_usd"])
        self.remaining = max(Decimal(0), self.remaining - run["command_cost_usd"])
        return run, rows


def relevant_account(account, terms):
    text = " ".join(account[k] or "" for k in ("nickname", "verification_text", "bio")).casefold()
    return any(term.strip() and term.strip().casefold() in text for term in terms)


def discover(brand, source, terms, budget, limit):
    peers = {}
    found = set()
    for term in dict.fromkeys(terms):
        for platform in ADAPTERS:
            if platform == source or platform in found:
                continue
            hits = [
                a
                for a in search_accounts(platform, term, budget, limit)
                if relevant_account(a, [brand, *terms])
            ]
            for account in hits:
                peers[platform, account["id"]] = account
            if hits:
                found.add(platform)
    return list(peers.values())


def search_accounts(platform, term, budget, limit):
    payload = (
        {"operation": "channelSearch", "keywords": [term], "maxPages": 1}
        if platform == "channels"
        else {"operation": "search_users", "keyword": term, "max_items": limit}
    )
    run, rows = budget.request(platform, payload, limit)
    peers = {}

    def field(row, *keys):
        return next((row[k] for k in keys if isinstance(row.get(k), str) and row[k]), None)

    for row in rows[:limit]:
        identifiers = channels_account_ids(row) if platform == "channels" else {}
        fields = {
            "douyin": ("sec_user_id", "author_sec_user_id"),
            "xiaohongshu": ("user_id", "author_user_id"),
            "channels": tuple(identifiers),
        }
        uid = next((str(row[k]) for k in fields[platform] if row.get(k)), None)
        if not uid:
            continue
        name = field(
            row,
            "nickname",
            "name",
            "author_name",
            "creatorName",
            "channelName",
            "channelTitle",
            "title",
        )
        verification = field(
            row,
            "verifiedEntity",
            "verification_text",
            "verification_label",
            "author_enterprise_verify_reason",
            "enterprise_verify_reason",
            "author_custom_verify",
        )
        bio = field(row, "bio", "introduction", "desc", "description", "signature")
        url = clean(field(row, "profile_url", "author_profile_url", "creatorUrl", "url"))
        followers = row.get("follower_count", row.get("fansCount", row.get("fans_count")))
        metadata = (
            dict(identifiers, follower_count=followers) if followers is not None else identifiers
        )
        upsert_account(platform, uid, name, url, verification, bio, metadata)
        peers[uid] = {
            "platform": platform,
            "id": uid,
            "nickname": name,
            "verification_text": verification,
            "bio": bio,
            "profile_url": url,
            "followers": followers,
        }
    if run:
        outcome = "sparse" if rows else "empty"
        if any(relevant_account(a, [term]) for a in peers.values()):
            outcome = "candidate"
        elif peers and all(
            a["nickname"] or a["verification_text"] or a["bio"] for a in peers.values()
        ):
            outcome = "no_relevant"
        with connect() as db:
            db.execute(
                "UPDATE catering_watch.fetch_runs SET metadata=metadata||%s WHERE id=%s",
                (
                    Jsonb(
                        {
                            "query": term,
                            "outcome": outcome,
                            "run_id": run["id"],
                        }
                    ),
                    run["fetch_id"],
                ),
            )
    return list(peers.values())


def sync(brand, platform, uid, budget, limit, persist, *, backfill=False):
    key = (settings.project_key, brand, platform, uid)
    if backfill and platform == "channels":
        raise ValueError("视频号仅提供最新一页，无法按游标回填")
    if budget.execute:
        register(brand, platform, uid)
    state = None
    if budget.execute or (settings.database_url and "<" not in settings.database_url):
        with connect() as db:
            state = db.execute(
                """SELECT next_cursor,backfill_done,extract(epoch from now()-interval '6 months') AS cutoff
                FROM catering_watch.tracked_accounts WHERE project_key=%s AND brand=%s AND platform=%s
                AND stable_id=%s""",
                key,
            ).fetchone()
    if backfill and state and state["backfill_done"]:
        return []
    cursor = state["next_cursor"] if backfill and state else None
    cutoff = state["cutoff"] if state else None
    adapter = ADAPTERS[platform]()
    payload = {
        "douyin": {"operation": "list_user_videos", "sec_user_id": uid},
        "channels": adapter.profile_payload(uid, limit),
        "xiaohongshu": {"operation": "list_user_notes", "user_id": uid},
    }[platform]
    ids = []
    remaining = limit
    while remaining > 0:
        if platform != "channels":
            payload = dict(payload, max_items=remaining, auto_paginate=False)
            if cursor:
                payload["page_token"] = cursor
        run, rows = budget.request(platform, payload, remaining)
        if not run:
            break
        items = [item for row in rows if (item := adapter.normalize(row))]
        recent = [
            i for i in items if i.published_at is None or cutoff is None or i.published_at >= cutoff
        ]
        next_cursor = next(
            (
                str(r["page_next_page_token"])
                for r in reversed(rows)
                if r.get("page_has_more") is not False and r.get("page_next_page_token")
            ),
            None,
        )
        truncated = any(
            isinstance(r.get("page_item_count"), int) and r["page_item_count"] > len(rows)
            for r in rows
        )
        if any(
            i.published_at is not None
            and cutoff is not None
            and i.published_at < cutoff
            and not (i.raw.get("is_pinned") or i.raw.get("is_top"))
            for i in items
        ):
            next_cursor = None
        remaining -= max(1, len(rows))
        # A pinned, already-known post can precede new posts on the same page.
        known = False
        was_complete = bool(state and state["backfill_done"])
        if not backfill and (next_cursor or truncated) and (remaining > 0 or was_complete):
            ordinary = [
                i.post_id for i in recent if not (i.raw.get("is_pinned") or i.raw.get("is_top"))
            ]
            with connect() as db:
                known = db.execute(
                    """SELECT 1 FROM catering_watch.posts WHERE project_key=%s AND platform=%s
                    AND platform_post_id=ANY(%s) LIMIT 1""",
                    (settings.project_key, platform, ordinary),
                ).fetchone()
        for item in recent:
            item.author_id = item.author_id or uid
            item.raw["collection_run_id"] = run["id"]
            ids.append(persist(item))
        incomplete = truncated or (cursor is not None and next_cursor == cursor)
        with connect() as db:
            db.execute(
                """UPDATE catering_watch.tracked_accounts SET synced_at=now()
                WHERE project_key=%s AND brand=%s AND platform=%s AND stable_id=%s""",
                key,
            )
            if platform != "channels":
                # A fresh head page may seed history but cannot rewind an existing bookmark.
                db.execute(
                    """UPDATE catering_watch.tracked_accounts SET next_cursor=%s,backfill_done=%s
                    WHERE project_key=%s AND brand=%s AND platform=%s AND stable_id=%s
                    AND (NOT backfill_done OR %s) AND next_cursor IS NOT DISTINCT FROM %s""",
                    (
                        cursor if incomplete else next_cursor,
                        not incomplete and next_cursor is None,
                        *key,
                        was_complete and not known,
                        cursor,
                    ),
                )
        if incomplete:
            if backfill:
                raise ValueError(
                    "作品已保存，历史游标保留在本页。请检查平台游标或提高 --max-items 和费用上限后重试。"
                )
            break
        if platform == "channels" or not next_cursor or known:
            break
        cursor = next_cursor
    return ids
