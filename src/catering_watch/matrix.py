"""Evidence-based video coverage and direct source-to-target repost reports."""

import json
import re
import os
from pathlib import Path

from rapidfuzz.fuzz import ratio

from .matching import normalize_text

PLATFORMS = ("douyin", "channels", "xiaohongshu")
CATALOG = json.loads(
    Path(
        os.environ.get("COMPETITOR_CATALOG") or Path(__file__).with_name("competitors.json")
    ).read_text(encoding="utf-8")
)
FIRST_CHECKS = json.loads(
    Path(__file__).with_name("first_run_checks.json").read_text(encoding="utf-8")
)
REPORT_FIELDS = {
    "reposts": [
        "competitor",
        "source_platform",
        "source",
        *PLATFORMS,
        "suspected_platform_count",
        "conclusion",
        "window_start",
        "window_end",
    ]
}
FIRST_RUN = "https://github.com/linjunsen470-a11y/social-monitor/blob/main/FIRST_RUN_RESULTS.md"
GENERIC = re.compile(
    r"有限公司|餐饮管理|团餐配送|团餐|食堂承包|广州|广东|员工餐|餐饮|配送|工作餐|企业|食堂|美食|后厨|厨房|现场|每天|新鲜|现炒|菜品|定制|\s+"
)


def search_terms(brand):
    return list(dict.fromkeys([brand["name"], *brand.get("entities", []), *brand["aliases"]]))


def seed_brand(platform, pid):
    return next((b for b in CATALOG if pid in b.get("seeds", {}).get(platform, [])), None)


def video_status(post):
    typ = post["metadata"].get("media_type")
    if typ:
        return typ == "video"
    if post["duration_seconds"] or post["metadata"].get("source") == "WeChat Channels videoSearch":
        return True
    return None


def ownership(brand, post):
    if not post["author_id"]:
        return None
    if post["verification_text"] and post["verification_text"] in brand.get("entities", []):
        return post["verification_text"]
    for account in brand.get("accounts", []):
        if (account["platform"], account["id"]) == (post["platform"], post["author_id"]):
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
        ownership(brand, post)
        or post["platform_post_id"] in brand.get("seeds", {}).get(post["platform"], [])
        or any(term.casefold() in text for term in search_terms(brand))
    )


def distinctive(value, brand):
    text = normalize_text(value)
    for term in sorted(search_terms(brand), key=len, reverse=True):
        text = text.replace(normalize_text(term), "")
    return GENERIC.sub("", text)


def repost_reasons(source, target, brand):
    hashes = [p["cover_phash"] for p in (source, target)]
    if not all(re.fullmatch(r"[0-9a-fA-F]{16}", h or "") for h in hashes):
        return []
    distance = (int(hashes[0], 16) ^ int(hashes[1], 16)).bit_count()
    if distance > 8:
        return []
    a, b = [p["duration_seconds"] for p in (source, target)]
    if a and b and abs(float(a) - float(b)) > max(2, 0.05 * min(float(a), float(b))):
        return []
    left, right = (
        [distinctive(p[k], brand) for k in ("title", "body_text")] for p in (source, target)
    )
    if not any(min(len(x), len(y)) >= 8 and ratio(x, y) >= 88 for x in left for y in right):
        return []
    reasons = [
        f"封面指纹距离 {distance}/64",
        "独特文案高度相似",
        "时长接近" if a and b else "时长缺失",
    ]
    if source["published_at"] and target["published_at"]:
        gap = (target["published_at"] - source["published_at"]).total_seconds() / 3600
        reasons.append(f"目标作品发布时间相对源作品 {gap:+.1f} 小时")
    return reasons


def evidence(p, owner):
    return {
        "id": p["platform_post_id"],
        "url": p["post_url"],
        "published_at": p["published_at"],
        "duration_seconds": p["duration_seconds"],
        "title": p["title"],
        "account_id": p["author_id"],
        "operator_evidence": owner,
        "video": video_status(p),
    }


def reports(kind, posts, runs, catalog=CATALOG):
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
                count = 0
                for platform, candidates in by_platform.items():
                    matches = []
                    if owner and platform != source["platform"]:
                        for p in candidates:
                            target_owner = ownership(brand, p)
                            if not target_owner or video_status(p) is not True:
                                continue
                            reasons = repost_reasons(source, p, brand)
                            if reasons:
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
                        "source_url": meta.get("source_url"),
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
                c["outcome"] in ("sparse", "unknown", "grouped") and c["status"] != "FAILED"
                for c in checks
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


def historical_checks(start, end):
    from datetime import datetime, timezone

    observed = datetime(2026, 11, 26, 7, 5, tzinfo=timezone.utc)
    if not start <= observed <= end:
        return []
    return [
        {
            "platform": platform,
            "status": "SUCCEEDED",
            "finished_at": observed,
            "metadata": {"query": name, "outcome": outcome, "source_url": FIRST_RUN},
        }
        for name, platform, outcome in FIRST_CHECKS
    ]


def read_report(kind):
    from .db import POST_SELECT, connect, settings

    with connect() as conn:
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
            WHERE project_key=%s AND operation='keyword_search' AND finished_at BETWEEN %s AND %s
            ORDER BY finished_at""",
            (settings.project_key, window["start"], window["finish"]),
        ).fetchall()
    return assemble_report(kind, posts, runs, window, settings.project_key)


def assemble_report(kind, posts, runs, window, project_key):
    start, end = window["start"], window["finish"]
    history = (
        historical_checks(start, end) if project_key == "guangzhou_catering_competitors" else []
    )
    recent = [p for p in posts if p["published_at"] and start <= p["published_at"] <= end]
    rows = reports(kind, recent, history + runs)
    if kind == "coverage":
        for brand, row in zip(CATALOG, rows):
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
