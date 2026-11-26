"""Keyword-first catering research and database-backed evidence workbooks."""

from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from psycopg.types.json import Jsonb

from .adapters import ChannelsAdapter, channels_account_ids
from .apify import ApifyError, safe_error
from .config import settings
from .db import connect, stored_post
from .ingest import clean, replay_fields
from .matrix import CATALOG, ownership, search_outcome, search_terms
from .research_adapters import PEER_SEARCH
from .tracking import catalog_for


def validate_review(notes):
    """Validate optional review inputs before any paid lookup."""
    if not isinstance(notes, dict):
        raise TypeError("人工核对文件须为 JSON 对象")
    for field in ("selection", "terms", "video_links", "reviews"):
        if not isinstance(notes.get(field, {}), dict):
            raise TypeError(f"{field} 须为 JSON 对象")

    def text(value):
        return isinstance(value, str) and bool(value.strip())

    def public_url(value):
        return (
            text(value)
            and urlsplit(value).scheme in ("http", "https")
            and bool(urlsplit(value).hostname)
        )

    if not all(text(v) for v in notes.get("selection", {}).values()):
        raise ValueError("每条所选作品需要非空品牌或昵称")
    if not all(public_url(v) for v in notes.get("video_links", {}).values()):
        raise ValueError("人工作品链接需要 HTTP(S) 地址")
    for terms in notes.get("terms", {}).values():
        if not isinstance(terms, dict) or not all(
            k in PEER_SEARCH and text(v) for k, v in terms.items()
        ):
            raise ValueError("terms 需要平台名及非空检索词")
    for entry in notes.get("reviews", {}).values():
        if not isinstance(entry, dict) or not text(entry.get("conclusion")):
            raise ValueError("人工核对结论需要填写 conclusion")
        urls = entry.get("urls", [])
        if not isinstance(urls, list) or not all(public_url(v) for v in urls):
            raise ValueError("urls 需要公开依据链接数组")


class Research:
    def __init__(self, budget, persist, replay=None):
        self.budget = budget
        self.persist = persist
        self.archive = None
        if replay is not None:
            self.archive = {}
            for index, entry in enumerate(replay, start=1):
                if not isinstance(entry, dict) or not all(
                    isinstance(entry.get(k), str) and entry[k].strip()
                    for k in ("platform", "operation", "query", "run_id")
                ):
                    raise ValueError(f"回放第 {index} 条需要 platform、operation、query 和 run_id")
                key = (entry["platform"], entry["operation"], entry["query"])
                rows = entry.get("rows")
                if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                    raise ValueError(f"回放第 {index} 条的 rows 须为对象数组")
                if key in self.archive:
                    raise ValueError(f"重复的回放检索 {key}")
                self.archive[key] = entry
        self.checks = []
        self.incomplete = False

    def lookup(self, adapter, operation, term, payload, limit):
        platform = adapter.platform
        check = {"platform": platform, "operation": operation, "query": term}
        self.checks.append(check)
        if self.archive is not None:
            record = self.archive.get((platform, operation, term))
            if record is None:
                check["status"] = "缺少回放记录"
                self.incomplete = True
                return []
            check["run_id"] = record["run_id"]
            if record.get("status", "SUCCEEDED") != "SUCCEEDED":
                check["status"] = "原采集失败"
                self.incomplete = True
                return []
            rows = [replay_fields(adapter.prepare_row(r), operation) for r in record["rows"]]
        else:
            try:
                run, rows = self.budget.request(platform, payload, limit, adapter=adapter)
            except (ApifyError, httpx.HTTPError) as exc:
                # Keep successful platforms available. Never log a signed URL or token.
                check["status"] = f"采集失败 ({safe_error(exc)})"
                self.incomplete = True
                return []
            if not run:
                check["status"] = "预算不足"
                self.incomplete = True
                return []
            check.update(run_id=run["id"], charged_or_held_usd=str(run["command_cost_usd"]))
        items = [item for r in rows[:limit] if (item := adapter.normalize(r))]
        if self.archive is None and operation in ("videoSearch", "public_post_search"):
            with connect() as db:
                db.execute(
                    "UPDATE catering_watch.fetch_runs SET metadata=metadata || %s WHERE id=%s",
                    (
                        Jsonb(
                            {
                                "run_id": run["id"],
                                "outcome": search_outcome(term, items, len(rows[:limit])),
                            }
                        ),
                        run["fetch_id"],
                    ),
                )
        for item in items:
            item.raw["collection_run_id"] = check["run_id"]
        check["status"] = "有候选" if items else "已查无结果"
        if len(items) < len(rows[:limit]):
            check["status"] = "部分数据缺少作品 ID"
            self.incomplete = True
        check["count"] = len(items)
        return items

    def collect(self, queries, limit, max_details, peer_limit, review):
        channels = ChannelsAdapter()
        sources = {}
        selected = review.get("selection")
        brands = {}
        terms = review.get("terms", {})
        for query in dict.fromkeys(queries):
            payload = channels.search_payload(query, limit, sort="relevance", published="all")
            for item in self.lookup(channels, "videoSearch", query, payload, limit):
                eid = item.raw.get("exportId")
                if selected is not None and eid not in selected:
                    continue
                key = eid or item.post_id
                if key in sources:
                    continue
                brand = selected[eid] if selected is not None else item.author_name
                sources[key] = (brand, item, query)
        missing = set(selected or {}) - sources.keys()
        if missing:
            self.checks.append(
                {
                    "platform": "channels",
                    "operation": "videoSearch",
                    "query": ", ".join(sorted(missing)),
                    "status": "缺少所选作品",
                    "count": len(missing),
                }
            )
            self.incomplete = True

        videos = []
        for brand, item, query in sources.values():
            eid = item.raw.get("exportId")
            search_run = item.raw["collection_run_id"]
            detail_run = None
            if self.archive is None and not item.author_id:
                previous = stored_post("channels", item.post_id, item.raw)
                if previous and previous["author_id"]:
                    item.author_id = previous["author_id"]
                    if not item.author_name:
                        item.author_name = previous["author_name"]
                    item.raw.update(
                        channels_account_ids(previous["metadata"].get("raw_payload", {}))
                    )
            if max_details and eid and not item.author_id:
                max_details -= 1
                rows = self.lookup(channels, "videoDetail", eid, channels.reference_payload(eid), 1)
                if rows:
                    detail = rows[0]
                    # The request has exactly one source ID. Reject a contradictory response.
                    if any(
                        item.raw.get(k)
                        and detail.raw.get(k)
                        and str(item.raw[k]) != str(detail.raw[k])
                        for k in ("exportId", "docID", "hashDocID", "videoId")
                    ):
                        self.checks[-1]["status"] = "详情标识冲突"
                        self.incomplete = True
                    else:
                        detail_run = detail.raw["collection_run_id"]
                        raw = item.raw | replay_fields(detail.raw)
                        if detail.title:
                            raw["title"] = detail.title
                        item = channels.normalize(raw)
            brand = brand or item.author_name
            if brand:
                brands.setdefault(brand, {})
            else:
                self.checks.append(
                    {
                        "platform": "channels",
                        "operation": "discover-peers",
                        "query": item.post_id,
                        "status": "缺少品牌或发布者线索",
                    }
                )
                self.incomplete = True
            self.persist(item)
            videos.append(
                dict(
                    evidence(brand, item),
                    keyword=query,
                    export_id=eid,
                    search_run=search_run,
                    detail_run=detail_run,
                    reviewed_url=review.get("video_links", {}).get(eid),
                )
            )

        with connect() as db:
            catalog = catalog_for(db, CATALOG)
        peers = []
        for brand, statuses in brands.items():
            for platform, cls in PEER_SEARCH.items():
                adapter = cls()
                term = terms.get(brand, {}).get(platform, brand)
                items = self.lookup(
                    adapter,
                    "public_post_search",
                    term,
                    adapter.search_payload(term, peer_limit),
                    peer_limit,
                )
                statuses[platform] = self.checks[-1]["status"]
                for item in items:
                    self.persist(item)
                    peer = evidence(brand, item)
                    owner_post = {
                        "platform": platform,
                        "author_id": item.author_id,
                        "verification_text": peer["verification"],
                    }
                    proofs = {
                        proof
                        for entry in catalog
                        if brand in search_terms(entry) and (proof := ownership(entry, owner_post))
                    }
                    if len(proofs) == 1:
                        peer["ownership_evidence"] = proofs.pop()
                        statuses[platform] = self.checks[-1]["status"] + "；有自营依据"
                    peers.append(peer)

        return {
            "videos": videos,
            "peers": peers,
            "brands": brands,
            "checks": self.checks,
            "reviews": {k: v for k, v in review.get("reviews", {}).items() if k in brands},
            "complete": not self.incomplete,
        }


def evidence(brand, item):
    return {
        "brand": brand,
        "platform": item.platform,
        "post_id": item.post_id,
        "name": item.author_name,
        "account_id": item.author_id,
        "url": clean(item.url),
        "title": item.title,
        "body": item.body,
        "published_at": item.published_at,
        "verification": item.raw.get("verification_text"),
        "run_id": item.raw["collection_run_id"],
    }


def save_report(result):
    report_id = uuid4()
    with connect() as db:
        db.execute(
            "INSERT INTO catering_watch.research_reports(id,project_key,evidence) VALUES(%s,%s,%s)",
            (report_id, settings.project_key, Jsonb(result)),
        )
    return report_id


def export_report(report_id, output: Path):
    with connect() as db:
        row = db.execute(
            "SELECT evidence FROM catering_watch.research_reports WHERE project_key=%s AND id=%s",
            (settings.project_key, report_id),
        ).fetchone()
    if not row:
        raise ValueError("当前项目中没有该调研报告")
    data = row["evidence"]
    book = Workbook()
    book.remove(book.active)

    def sheet(name, headers, rows):
        tab = book.create_sheet(name)
        tab.append(headers)
        for values in rows:
            tab.append(
                [ILLEGAL_CHARACTERS_RE.sub("", v) if isinstance(v, str) else v for v in values]
            )
            # Captions and account names are public user input, including strings starting with '='.
            for cell in tab[tab.max_row]:
                if isinstance(cell.value, str):
                    cell.data_type = "s"
                    if (
                        headers[cell.column - 1]
                        in {"公开链接", "人工核对链接", "样本作品链接", "公开依据", "自营依据"}
                        and cell.value.startswith(("https://", "http://"))
                        and not any(c.isspace() for c in cell.value)
                    ):
                        cell.hyperlink = cell.value
                        cell.style = "Hyperlink"
        tab.freeze_panes = "A2"
        tab.auto_filter.ref = tab.dimensions

    counts = Counter(v["brand"] for v in data["videos"])
    sheet(
        "竞品三端核查",
        ["品牌或账号线索", "视频号作品数", "抖音检索", "小红书检索", "人工核对结论", "公开依据"],
        (
            [
                brand,
                counts[brand],
                states["douyin"],
                states["xiaohongshu"],
                data["reviews"].get(brand, {}).get("conclusion", "归属待核实"),
                "\n".join(data["reviews"].get(brand, {}).get("urls", [])),
            ]
            for brand, states in data["brands"].items()
        ),
    )
    columns = {
        "brand": "竞品线索",
        "platform": "平台",
        "name": "发布者",
        "account_id": "账号 ID",
        "post_id": "作品 ID",
        "url": "公开链接",
        "title": "标题",
        "body": "文案",
        "published_at": "发布时间（Unix 秒）",
        "verification": "平台返回的认证文本",
        "run_id": "运行 ID",
    }
    for name, key, extra in [
        (
            "微信视频作品",
            "videos",
            {
                "keyword": "关键词",
                "export_id": "exportId",
                "search_run": "搜索运行",
                "detail_run": "详情运行",
                "reviewed_url": "人工核对链接",
            },
        ),
        ("跨平台作品证据", "peers", {"ownership_evidence": "自营依据"}),
    ]:
        fields = columns | extra
        sheet(name, list(fields.values()), ([r.get(k) for k in fields] for r in data[key]))
    sheet(
        "策略观察",
        ["采购关切", "作品中出现的用语", "命中微信作品数", "样本作品链接"],
        (
            strategy_row(topic, terms, data["videos"])
            for topic, terms in [
                ("采购成本", ("元", "餐标", "成本")),
                ("试餐与考察", ("试餐", "试菜", "考察", "看厨")),
                ("配送与履约", ("配送", "分餐", "留样", "食品安全")),
            ]
        ),
    )
    fields = ["platform", "operation", "query", "status", "count", "run_id", "charged_or_held_usd"]
    sheet(
        "检索记录",
        ["平台", "操作", "查询", "状态", "候选数", "运行 ID", "本次扣费或预留 USD"],
        ([r.get(k) for k in fields] for r in data["checks"]),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    book.save(output)


def strategy_row(topic, terms, videos):
    hits = [r for r in videos if any(t in (r["title"] or "") + (r["body"] or "") for t in terms)]
    return [topic, "、".join(terms), len(hits), next((r["url"] for r in hits if r["url"]), None)]
