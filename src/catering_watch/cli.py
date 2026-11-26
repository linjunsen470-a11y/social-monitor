from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import typer
from rich import print

from . import tracking
from .adapters import ADAPTERS, channels_account_ids, is_profile_reference
from .apify import ApifyClient
from .config import settings
from .db import (
    connect,
    db_status,
    find_cached_fetch,
    get_cover_phash,
    get_posts_by_ids,
    init_db,
    save_candidate_match,
    save_cover_phash,
    stored_post,
    upsert_post,
)
from .ingest import clean, cover_url, key_for, replay_fields
from .matching import compare_posts, cover_feature
from .matrix import CATALOG, REPORT_FIELDS, read_report, search_terms, seed_brand

app = typer.Typer(
    no_args_is_help=True, help="Agent-first three-platform competitor content research"
)


def _fingerprint(*parts):
    return hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _budget_check(max_cost, execute):
    if max_cost is None:
        if execute:
            raise typer.BadParameter("执行采集需要显式指定 --max-cost")
    elif not 0 < max_cost <= settings.max_cost_usd:
        raise typer.BadParameter(
            f"--max-cost 必须在 0 与配置上限 ${settings.max_cost_usd:.2f} 之间"
        )
    if not execute:
        print("[yellow]DRY RUN：没有调用 Apify。添加 --execute 才会执行。[/yellow]")
        return False
    return True


def _persist(item, video_search=False):
    raw = item.raw
    declared = str(raw.get("note_type") or raw.get("type") or "").lower()
    media_type = "unknown"
    if item.platform == "xiaohongshu" and declared in {"normal", "image", "photo", "图文"}:
        media_type = "image"
    elif (
        video_search or item.duration_seconds or declared == "video" or item.platform == "channels"
    ):
        media_type = "video"
    metadata = {"raw_payload": replay_fields(raw), "publisher": item.author_name}
    if media_type != "unknown":
        metadata["media_type"] = media_type
    verification = (
        raw.get("verification_text")
        or raw.get("author_custom_verify")
        or raw.get("author_enterprise_verify_reason")
        or raw.get("enterprise_verify_reason")
    )

    image_url = cover_url(item.cover_url)
    post_id = upsert_post(
        {
            "platform": item.platform,
            "post_id": item.post_id,
            "url": clean(item.url),
            "published_at": item.published_at,
            "title": item.title,
            "body": item.body,
            "cover_url": image_url,
            "duration_seconds": item.duration_seconds,
            "author_name": item.author_name,
            "author_id": item.author_id,
            "author_identifiers": channels_account_ids(raw)
            if item.platform == "channels"
            else None,
            "author_url": clean(item.author_url),
            "verification": verification if isinstance(verification, str) else None,
            "metadata": metadata,
        }
    )

    _ensure_cover(post_id, image_url, get_cover_phash(post_id), item.platform)
    return post_id


def _export(rows):
    out = Path("outputs")
    out.mkdir(exist_ok=True)
    with (out / "latest_scan.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    keys = list(dict.fromkeys(k for row in rows for k in row.keys()))
    with (out / "latest_scan.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys or ["status"])
        writer.writeheader()
        writer.writerows(rows)


def _ensure_cover(post_id, url, existing, platform):
    if existing:
        return existing
    value, status = cover_feature(url, platform)
    with connect() as db:
        db.execute(
            """UPDATE catering_watch.posts SET metadata=metadata || jsonb_build_object('cover_status',%s::text) WHERE project_key=%s AND id=%s""",
            (status, settings.project_key, post_id),
        )
    save_cover_phash(post_id, value)
    return value


def _search_outcome(query, items, raw_count):
    if not items:
        return "sparse" if raw_count else "empty"
    brand = next((b for b in CATALOG if query in search_terms(b)), None)
    terms = search_terms(brand) if brand else [query]
    for item in items:
        description = " ".join(filter(None, (item.author_name, item.title, item.body))).casefold()
        if any(term.casefold() in description for term in terms):
            return "candidate"
    if raw_count > len(items):
        return "sparse"
    return "no_relevant"


def _cross_search(source_platform, query, max_items, max_cost, source_post=None):
    budget = tracking.Budget(max_cost, execute=True)
    build = os.getenv("APIFY_BUILD", "latest")
    output = []
    for target in (p for p in ADAPTERS if p != source_platform):
        try:
            adapter = ADAPTERS[target]()
            payload = adapter.search_payload(query, max_items)
            keys = [key_for(adapter.actor, payload, build)]
            if build == "latest":
                # main stored normalized post IDs under its pre-PR scan fingerprint.
                keys.append(
                    _fingerprint(target, adapter.actor, query, max_items, "half_year", "video")
                )
            cached = find_cached_fetch(keys)
            ids = cached["metadata"]["post_db_ids"] if cached else []
            posts = get_posts_by_ids(ids)
            cache_hit = bool(cached) and len(posts) == len(set(ids))
            if cache_hit:
                print(f"[cyan]CACHE HIT[/cyan] {target}，复用 {len(posts)} 条作品")
            else:
                run, raw = budget.request(target, payload, max_items)
                if not run:
                    print(f"{target} 预算不足，跳过检索")
                    continue
                items = [item for row in raw if (item := adapter.normalize(row))]
                ids = []
                for item in items:
                    item.raw["collection_run_id"] = run["id"]
                    ids.append(_persist(item, video_search=True))
                posts = get_posts_by_ids(ids)
                with connect() as db:
                    db.execute(
                        "UPDATE catering_watch.fetch_runs SET metadata=metadata || %s::jsonb WHERE id=%s",
                        (
                            json.dumps(
                                {
                                    "query": query,
                                    "run_id": run["id"],
                                    "outcome": _search_outcome(query, items, len(raw)),
                                    "post_db_ids": ids,
                                }
                            ),
                            run["fetch_id"],
                        ),
                    )
                print(f"{target}，{len(posts)} 条作品，本次计入额度 ${run['command_cost_usd']:.4f}")
            for post in posts:
                if cache_hit and post["cover_url"] and not post["cover_phash"]:
                    post["cover_phash"] = _ensure_cover(post["id"], post["cover_url"], None, target)
                match = None
                if source_post:
                    match = compare_posts(source_post, post)
                    save_candidate_match(
                        source_post["db_post_id"],
                        post["id"],
                        match,
                        source_post.get("url"),
                        post["post_url"],
                    )
                output.append(
                    {
                        "platform": post["platform"],
                        "post_id": post["platform_post_id"],
                        "url": post["post_url"],
                        "published_at": post["published_at"],
                        "title": post["title"],
                        "body": post["body_text"],
                        "author_name": post["author_name"],
                        "author_id": post["author_id"],
                        "match": match,
                        "cache_hit": cache_hit,
                    }
                )
        except Exception as exc:
            print(f"[red]{target} failed[/red]: {exc}")
    return output


@app.command()
def doctor(check_apify: bool = typer.Option(False, "--check-apify")):
    """Check configuration and Neon; no scraping."""
    print(
        {
            "database_configured": bool(
                settings.database_url and "<PASSWORD>" not in settings.database_url
            ),
            "apify_token_configured": bool(
                settings.apify_token and "<YOUR_TOKEN>" not in settings.apify_token
            ),
            "openai_key_configured": bool(settings.openai_api_key),
            "project_key": settings.project_key,
            "max_cost_usd": settings.max_cost_usd,
        }
    )
    if settings.database_url and "<PASSWORD>" not in settings.database_url:
        print({"neon": dict(db_status())})
    if check_apify:
        print({"apify": ApifyClient().check_auth()})


@app.command("init-db")
def init_db_command():
    """Apply idempotent schema to configured Neon database."""
    init_db()
    print("[green]Schema ready.[/green]")


@app.command("db-status")
def db_status_command():
    print(dict(db_status()))


@app.command()
def scan(
    platform: str = typer.Option(..., "--platform"),
    query: str = typer.Option(..., "--query"),
    max_items: int = typer.Option(None, "--max-items", min=1),
    max_cost: float = typer.Option(None, "--max-cost"),
    execute: bool = typer.Option(False, "--execute"),
):
    """Search the other two platforms from a platform/business/content clue."""
    if platform not in ADAPTERS:
        raise typer.BadParameter("Unsupported platform")
    if not _budget_check(max_cost, execute):
        print(
            {"source": platform, "targets": [p for p in ADAPTERS if p != platform], "query": query}
        )
        return
    rows = _cross_search(platform, query, max_items or settings.max_items_per_search, max_cost)
    _export(rows)
    print(f"Exported {len(rows)} candidate rows.")


@app.command("scan-url")
def scan_url(
    url: str = typer.Option(..., "--url"),
    platform: str = typer.Option("", "--platform"),
    query: str = typer.Option("", "--query"),
    max_items: int = typer.Option(3, "--max-items", min=1),
    max_cost: float = typer.Option(None, "--max-cost"),
    execute: bool = typer.Option(False, "--execute"),
):
    """Start from any public post/profile URL and search the other two platforms."""
    source = platform or tracking.platform_for(url)
    if source not in ADAPTERS:
        raise typer.BadParameter("Unsupported platform")
    _budget_check(max_cost, execute)
    budget = tracking.Budget(max_cost, execute)
    if execute:
        db_status()
        if "://" in url:
            url = tracking.resolve(url, source)
    match = re.search(r"(?:/video/|modal_id=)([0-9]+)", url) if source == "douyin" else None
    existing = None
    if match and (execute or (settings.database_url and "<" not in settings.database_url)):
        existing = stored_post(source, match[1])
    adapter = ADAPTERS[source]()
    profile = is_profile_reference(source, url)
    limit = max_items if profile else 1
    payload = adapter.profile_payload(url, limit) if profile else adapter.reference_payload(url)
    if existing:
        sources = [existing]
    else:
        run, raw = budget.request(source, payload, limit)
        items = [item for row in raw if (item := adapter.normalize(row))]
        if not (profile and source == "channels"):
            items = items[:limit]
        for item in items:
            item.raw["collection_run_id"] = run["id"]
        sources = get_posts_by_ids([_persist(i) for i in items])
    if not execute:
        brand = seed_brand(source, match[1]) if match else None
        terms = [query] if query else search_terms(brand) if brand else ["<resolved account name>"]
        tracking.discover("", source, terms, budget, max_items)
        print(
            {
                "source": source,
                "targets": [p for p in ADAPTERS if p != source],
                "queries": terms,
                "operations": budget.steps,
                "source_cached": bool(existing),
                "sync": "同步已登记账号及认证主体完全匹配的账号，其他候选供选择",
            }
        )
        return
    if not sources:
        print(budget.steps)
        print("源入口缺少可用作品数据。")
        return
    first = sources[0]
    brand = seed_brand(source, first["platform_post_id"])
    if not brand:
        brand = next(
            (b for b in CATALOG if (query or first["author_name"]) in search_terms(b)), None
        )
    name = (brand or {}).get("name") or query or first["author_name"]
    if not name:
        raise typer.BadParameter("源作品缺少账号名，请指定 --query")
    for post in sources:
        if post["author_id"]:
            tracking.register(name, source, post["author_id"])
        if existing:
            _ensure_cover(post["id"], post["cover_url"], post["cover_phash"], source)
    terms = [query] if query else search_terms(brand) if brand else [name]
    peers = tracking.discover(name, source, terms, budget, max_items)
    with connect() as db:
        catalog = tracking.catalog_for(db, CATALOG)
    entry = next((b for b in catalog if b["name"] == name), {})
    targets = {
        (a["platform"], a["id"]) for a in entry.get("accounts", []) if a["platform"] != source
    }
    for peer in peers:
        if peer["verification_text"] and peer["verification_text"] in (brand or {}).get(
            "entities", []
        ):
            targets.add((peer["platform"], peer["id"]))
    if peers:
        print({"候选账号": peers})
    for target, uid in sorted(targets):
        tracking.sync(name, target, uid, budget, max_items, _persist)
    rows = [r for r in read_report("reposts") if r["competitor"] == name]
    _export(rows)
    print(budget.steps)


def _budget(platform, max_cost, execute):
    if platform not in ADAPTERS:
        raise typer.BadParameter("Unsupported platform")
    _budget_check(max_cost, execute)
    return tracking.Budget(max_cost, execute)


@app.command("track-account")
def track_account(
    brand: str,
    platform: str,
    reference: str,
    kind: str = "id",
    evidence_url: str = "",
    execute: bool = False,
    max_cost: float = typer.Option(None, min=0),
):
    """Register a stable account or search a nickname and display selectable IDs."""
    if platform not in ADAPTERS:
        raise typer.BadParameter("Unsupported platform")
    if kind == "name":
        budget = _budget(platform, max_cost, execute)
        print(tracking.search_accounts(platform, reference, budget, 5))
        print(budget.steps)
        return
    if kind == "url":
        if tracking.platform_for(reference) != platform:
            raise typer.BadParameter("平台与主页链接不符")
        if not execute:
            print({"platform": platform, "profile": reference, "brand": brand})
            return
        url = tracking.resolve(reference, platform)
        parts = urlsplit(url)
        if not is_profile_reference(platform, url):
            raise typer.BadParameter("需要账号主页或 --kind id")
        if platform == "channels":
            query = parse_qs(parts.query)
            reference = next(
                iter(channels_account_ids({k: v[0] for k, v in query.items()}).values()), None
            )
            if not reference:
                raise typer.BadParameter("视频号链接未提供稳定账号 ID，请用 --kind name 查找")
        else:
            prefix = "/user/" if platform == "douyin" else "/user/profile/"
            match = re.match(re.escape(prefix) + r"([^/]+)(?:/|$)", parts.path)
            if not match:
                raise typer.BadParameter("主页链接缺少稳定账号 ID")
            reference = match[1]
    elif kind != "id":
        raise typer.BadParameter("kind must be id, url or name")
    if execute:
        tracking.register(brand, platform, reference, evidence_url or None)
    print({"brand": brand, "platform": platform, "account_id": reference})


@app.command("discover-peers")
def discover_peers(
    brand: str,
    platform: str,
    max_items: int = typer.Option(5, min=1),
    execute: bool = False,
    max_cost: float = typer.Option(None),
):
    budget = _budget(platform, max_cost, execute)
    terms = next((search_terms(b) for b in CATALOG if b["name"] == brand), [brand])
    print(tracking.discover(brand, platform, terms, budget, max_items))
    print(budget.steps)


@app.command("sync-account")
def sync_account(
    brand: str,
    platform: str,
    account_id: str,
    max_items: int = typer.Option(20, min=1),
    execute: bool = False,
    max_cost: float = typer.Option(None),
    backfill: bool = typer.Option(False, "--backfill", help="从保存的游标回填抖音或小红书历史作品"),
):
    budget = _budget(platform, max_cost, execute)
    if backfill and platform == "channels":
        raise typer.BadParameter("视频号仅提供最新一页，无法按游标回填")
    ids = tracking.sync(brand, platform, account_id, budget, max_items, _persist, backfill=backfill)
    print({"posts": len(ids), "operations": budget.steps})


@app.command("match-local")
def match_local(output: Path = Path("outputs/local-matches.jsonl")):
    """Recompute direct metadata matches from persisted evidence without paid calls."""

    def persist_pair(source, target, result):
        save_candidate_match(
            source["id"], target["id"], result, source["post_url"], target["post_url"]
        )

    write_report("reposts", "jsonl", output, read_report("reposts", on_match=persist_pair))


@app.command()
def batch(
    input: Path = typer.Option(..., "--input", exists=True, readable=True),
    execute: bool = typer.Option(False, "--execute"),
    max_cost: float = typer.Option(None, "--max-cost"),
):
    """CSV columns: platform,url,query,max_items. Dry-run by default."""
    with input.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not _budget_check(max_cost, execute):
        print(f"DRY RUN: {len(rows)} rows")
        [print(row) for row in rows]
        return
    if not rows:
        return
    per_row = max_cost / len(rows)
    for row in rows:
        url = (row.get("url") or "").strip()
        query = (row.get("query") or "").strip()
        platform = (row.get("platform") or "").strip()
        if url:
            scan_url(
                url=url,
                platform=platform,
                query=query,
                max_items=int(row.get("max_items") or 3),
                max_cost=per_row,
                execute=True,
            )
        elif platform in ADAPTERS and query:
            scan(
                platform=platform,
                query=query,
                max_items=int(row.get("max_items") or settings.max_items_per_search),
                max_cost=per_row,
                execute=True,
            )
        else:
            print(f"[yellow]Skip invalid CSV row: {row}[/yellow]")


@app.command()
def report(
    format: str = typer.Option("csv", "--format"),
    kind: str = typer.Option("matches", "--kind"),
    output: Path = typer.Option(Path("outputs/report.csv"), "--output"),
):
    """Export persisted accounts, posts or match candidates."""
    queries = {
        "posts": """SELECT platform,platform_post_id,post_url,published_at,title,body_text,cover_url,metadata,
        first_seen_at,last_seen_at FROM catering_watch.posts WHERE project_key=%s ORDER BY published_at DESC NULLS LAST""",
        "accounts": """SELECT platform,platform_account_id,current_name,profile_url,verification_text,bio,
        ownership_status,first_seen_at,last_seen_at FROM catering_watch.accounts WHERE project_key=%s ORDER BY platform,current_name""",
        "matches": """SELECT c.id AS candidate_id,sp.platform AS source_platform,sp.platform_post_id AS source_post_id,
        sp.post_url AS source_url,sp.title AS source_title,tp.platform AS target_platform,
        tp.platform_post_id AS target_post_id,tp.post_url AS target_url,tp.title AS target_title,c.score,c.status,
        m.video_relation,m.ownership_relation,m.confidence,m.rationale,c.first_seen_at,c.last_seen_at
        FROM catering_watch.candidates c JOIN catering_watch.posts sp ON sp.id=c.source_post_id
        JOIN catering_watch.posts tp ON tp.id=c.target_post_id LEFT JOIN catering_watch.matches m ON m.candidate_id=c.id
        WHERE c.project_key=%s ORDER BY c.last_seen_at DESC""",
    }
    if kind in {"coverage", "reposts"}:
        rows = read_report(kind)
    elif kind in queries:
        with connect() as conn:
            rows = conn.execute(queries[kind], (settings.project_key,)).fetchall()
    else:
        raise typer.BadParameter("kind must be coverage, reposts, posts, matches or accounts")
    write_report(kind, format, output, rows)


def write_report(kind, format, output, rows):
    output.parent.mkdir(parents=True, exist_ok=True)
    if format == "jsonl":
        with output.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(dict(row), default=str, ensure_ascii=False) + "\n")
    elif format == "csv":
        with output.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=list(dict.fromkeys(k for row in rows for k in row))
                if rows
                else REPORT_FIELDS.get(kind, ["kind"]),
            )
            writer.writeheader()
            writer.writerows(
                {
                    k: json.dumps(v, ensure_ascii=False, default=str)
                    if isinstance(v, (dict, list))
                    else v
                    for k, v in r.items()
                }
                for r in rows
            )
    else:
        raise typer.BadParameter("format must be csv or jsonl")
    print(f"Exported {len(rows)} {kind} rows to {output}")


if __name__ == "__main__":
    app()
