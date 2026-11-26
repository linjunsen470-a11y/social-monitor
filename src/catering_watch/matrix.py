"""Evidence-based video coverage and direct source-to-target repost reports."""

import json
import os
from pathlib import Path

from .adapters import channels_account_ids
from .matching import compare_posts
from .tracking import catalog_for

PLATFORMS = ("douyin", "channels", "xiaohongshu")
CATALOG = json.loads(
    Path(
        os.environ.get("COMPETITOR_CATALOG") or Path(__file__).with_name("competitors.json")
    ).read_text(encoding="utf-8")
)
REPORT_FIELDS = {
    "reposts": [
        "competitor",
        "source_platform",
        "source",
        *PLATFORMS,
        "review_candidates",
        "suspected_platform_count",
        "conclusion",
        "window_start",
        "window_end",
    ]
}


def search_terms(brand):
    return list(dict.fromkeys([brand["name"], *brand.get("entities", []), *brand["aliases"]]))


def search_outcome(query, items, raw_count):
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


def seed_brand(platform, pid):
    return next((b for b in CATALOG if pid in b.get("seeds", {}).get(platform, [])), None)


def video_status(post):
    typ = post["metadata"].get("media_type")
    if typ:
        return typ == "video"
    if post["duration_seconds"]:
        return True
    return None


def account_matches(account, post):
    if account["platform"] != post["platform"]:
        return False
    if post["platform"] != "channels":
        return account["id"] == post["author_id"]
    observed = channels_account_ids(post["metadata"].get("raw_payload", {}))
    return not {post["author_id"], *observed.values()}.isdisjoint(
        account.get("ids", [account["id"]])
    )


def ownership(brand, post):
    if not post["author_id"]:
        return None
    if post["verification_text"] and post["verification_text"] in brand.get("entities", []):
        return post["verification_text"]
    for account in brand.get("accounts", []):
        if account.get("evidence_url") and account_matches(account, post):
            return account["evidence_url"]
    return None


def relates(brand, post):
    fields = (
        post["author_name"],
        post["title"],
        post["body_text"],
        post["metadata"].get("publisher"),
    )
    text = " ".join(str(f) for f in fields if f).casefold()
    return (
        any(account_matches(a, post) for a in brand.get("accounts", []))
        or ownership(brand, post)
        or post["platform_post_id"] in brand.get("seeds", {}).get(post["platform"], [])
        or any(term.casefold() in text for term in search_terms(brand))
    )


def evidence(p, owner):
    return {
        "id": p["platform_post_id"],
        "url": p["post_url"],
        "published_at": p["published_at"],
        "duration_seconds": p["duration_seconds"],
        "title": p["title"],
        "account_id": p["author_id"],
        "operator_evidence": owner,
        "cover_status": p["metadata"].get("cover_status"),
        "run_id": p["metadata"].get("raw_payload", {}).get("collection_run_id"),
        "video": video_status(p),
    }


def reports(kind, posts, runs, catalog=CATALOG, on_match=None):
    rows = []
    for brand in catalog:
        related = [p for p in posts if video_status(p) is not False and relates(brand, p)]
        by_platform = {k: [p for p in related if p["platform"] == k] for k in PLATFORMS}
        if kind == "reposts":
            for source in related:
                if video_status(source) is not True:
                    continue
                owner = ownership(brand, source)
                row = {
                    "competitor": brand["name"],
                    "source_platform": source["platform"],
                    "source": evidence(source, owner),
                }
                row["review_candidates"] = []
                count = 0
                for platform, candidates in by_platform.items():
                    matches = []
                    if platform != source["platform"]:
                        for p in candidates:
                            target_owner = ownership(brand, p)
                            if video_status(p) is not True:
                                continue
                            result = compare_posts(source, p, brand)
                            if on_match:
                                on_match(source, p, result)
                            reasons = result["reasons"]
                            if result["relation"] != "not_matched_by_metadata" and not (
                                owner
                                and target_owner
                                and result["relation"] == "high_similarity_candidate"
                            ):
                                row["review_candidates"].append(
                                    dict(
                                        evidence(p, target_owner),
                                        platform=platform,
                                        reasons=reasons,
                                    )
                                )
                            elif result["relation"] == "high_similarity_candidate":
                                matches.append(dict(evidence(p, target_owner), reasons=reasons))
                    row[platform] = matches
                    count += bool(matches)
                row["suspected_platform_count"] = 1 + count if count else 0
                row["conclusion"] = "疑似整条重发" if count else "证据不足"
                rows.append(row)
            continue
        row = {"competitor": brand["name"], "owned_platform_count": 0}
        for platform, candidates in by_platform.items():
            checks = []
            for run in runs:
                meta = run["metadata"]
                if run["platform"] != platform or meta.get("query") not in search_terms(brand):
                    continue
                checks.append(
                    {
                        "status": run["status"],
                        "run_id": meta.get("run_id"),
                        "query": meta["query"],
                        "at": run["finished_at"],
                        "outcome": meta.get("outcome", "unknown"),
                    }
                )
            owned = [p for p in candidates if ownership(brand, p) and video_status(p) is True]
            if owned:
                status = "已观察到自营视频"
                row["owned_platform_count"] += 1
            elif candidates or any(c["outcome"] == "candidate" for c in checks):
                status = (
                    "归属待核实"
                    if not candidates or any(not ownership(brand, p) for p in candidates)
                    else "数据不足"
                )
            elif any(
                c["outcome"] in ("sparse", "unknown") and c["status"] != "FAILED" for c in checks
            ):
                status = "数据不足"
            elif any(c["outcome"] in ("empty", "no_relevant") for c in checks):
                status = "已查未检出"
            else:
                status = "查询失败" if checks else "尚未查询"
            row[platform] = status
            row[platform + "_evidence"] = [evidence(p, ownership(brand, p)) for p in candidates]
            row[platform + "_checks"] = checks
        rows.append(row)
    return rows


def read_report(kind, on_match=None):
    from .db import POST_SELECT, connect, settings

    with connect() as conn:
        catalog = catalog_for(conn, CATALOG)
        window = conn.execute(
            "SELECT now() AS finish, now()-interval '6 months' AS start"
        ).fetchone()
        posts = conn.execute(
            POST_SELECT
            + """
            AND (p.published_at BETWEEN %s AND %s OR p.platform_post_id=ANY(%s)
                 OR (p.published_at IS NULL AND p.last_seen_at BETWEEN %s AND %s)) ORDER BY p.platform, p.id""",
            (
                settings.project_key,
                window["start"],
                window["finish"],
                [
                    pid
                    for brand in CATALOG
                    for ids in brand.get("seeds", {}).values()
                    for pid in ids
                ],
                window["start"],
                window["finish"],
            ),
        ).fetchall()
        runs = conn.execute(
            """SELECT platform,status,finished_at,metadata FROM catering_watch.fetch_runs
            WHERE project_key=%s AND operation IN ('channelSearch','search_users','videoSearch','search_videos','search_notes','public_post_search') AND finished_at BETWEEN %s AND %s
            ORDER BY finished_at""",
            (settings.project_key, window["start"], window["finish"]),
        ).fetchall()
    return assemble_report(kind, posts, runs, window, catalog, on_match)


def assemble_report(kind, posts, runs, window, catalog=CATALOG, on_match=None):
    start, end = window["start"], window["finish"]
    recent = [p for p in posts if p["published_at"] and start <= p["published_at"] <= end]
    rows = reports(kind, recent, runs, catalog, on_match)
    if kind == "coverage":
        for brand, row in zip(catalog, rows):
            for post in posts:
                platform = post["platform"]
                if (
                    post["published_at"] is None
                    and video_status(post) is not False
                    and relates(brand, post)
                ):
                    row[platform + "_evidence"].append(evidence(post, ownership(brand, post)))
                    if row[platform] not in {"已观察到自营视频", "归属待核实"}:
                        row[platform] = "数据不足"
                    continue
                if (
                    post["platform_post_id"] not in brand.get("seeds", {}).get(post["platform"], [])
                    or not post["published_at"]
                    or post["published_at"] >= start
                ):
                    continue
                row.setdefault("historical_seeds", []).append(
                    dict(evidence(post, ownership(brand, post)), platform=post["platform"])
                )
                if row[post["platform"]] == "尚未查询":
                    row[post["platform"]] = "仅有窗口外历史样本"
            row.setdefault("historical_seeds", [])
    return [dict(row, window_start=start, window_end=end) for row in rows]
