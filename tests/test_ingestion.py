import os
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from catering_watch import ingest, tracking
from catering_watch.adapters import ChannelsAdapter, DouyinAdapter
from catering_watch.apify import ApifyError
from catering_watch.db import connect, init_db


@pytest.mark.parametrize("value", [1700000000, 1700000000000, "2023-11-14T22:13:20Z"])
def test_epoch(value):
    assert ingest.timestamp(value) == 1700000000


def test_distinct_channel_ids_and_credentials():
    adapter = ChannelsAdapter(client=object())
    assert adapter.normalize({"docID": "x"}).post_id != adapter.normalize({"exportId": "x"}).post_id
    assert ingest.clean({"auth_token": "secret", "url": "https://example.org/a?signature=s"}) == {
        "url": "https://example.org/a"
    }
    with pytest.raises(ValueError):
        tracking.platform_for("https://douyin.com.example.org/user/a")


@pytest.fixture
def project(monkeypatch):
    # Integration tests may create isolated records only in an explicit local test database.
    dsn = os.getenv("CATERING_TEST_DATABASE_URL", "")
    if not dsn.startswith("postgresql:///postgres?host=/tmp/"):
        pytest.skip("set CATERING_TEST_DATABASE_URL to an isolated local PostgreSQL Unix socket")
    from dataclasses import replace
    from catering_watch import db

    key = "test-" + uuid4().hex
    config = replace(db.settings, database_url=dsn, apify_token="test-local", project_key=key)
    for module in (db, ingest, tracking):
        monkeypatch.setattr(module, "settings", config)
    monkeypatch.setattr(ingest, "PROJECT", key)
    init_db()
    with connect() as conn:
        conn.execute("INSERT INTO catering_watch.projects VALUES(%s,'fixture')", (key,))
    return key


class Provider:
    def __init__(self):
        self.starts = 0
        self.offsets = []
        self.fail = True

    def start(self, *args):
        self.starts += 1
        return {"id": "provider", "status": "RUNNING"}

    def status(self, provider):
        return {"id": provider, "status": "SUCCEEDED", "defaultDatasetId": "dataset"}

    def page(self, dataset, offset, limit):
        self.offsets.append(offset)
        if offset and self.fail:
            self.fail = False
            raise OSError("simulated interruption")
        return [{"aweme_id": str(i)} for i in range(offset, min(offset + limit, 123))]


def test_resume_pages_and_ledger(project, monkeypatch):
    monkeypatch.setattr(ingest.time, "sleep", lambda _: None)
    transport = Provider()
    adapter = DouyinAdapter()
    client = ingest.DurableClient(adapter, transport)
    args = (adapter.actor, {"operation": "list_user_videos"}, 0.7)
    with pytest.raises(OSError):
        client.run(*args)
    run, rows = client.run(*args)
    assert len(rows) == 123 and run["id"] == "provider"
    assert client.run(*args) == (run, rows)
    assert transport.starts == 1 and transport.offsets == [0, 100, 100]
    with connect() as db:
        ledger = db.execute(
            "SELECT * FROM catering_watch.cost_ledger WHERE project_key=%s", (project,)
        ).fetchall()
    assert len(ledger) == 1 and ledger[0]["estimated"]


def test_concurrent_claim_and_unknown_start(project):
    entered, release = threading.Event(), threading.Event()

    class Unknown(Provider):
        def start(self, *args):
            self.starts += 1
            entered.set()
            assert release.wait(5)
            raise OSError("response lost")

    provider = Unknown()
    adapter = DouyinAdapter()
    client = ingest.DurableClient(adapter, provider)
    args = (adapter.actor, {"operation": "search_users"}, 0.6)
    with ThreadPoolExecutor() as executor:
        first = executor.submit(client.run, *args)
        assert entered.wait(5)
        with pytest.raises(ApifyError):
            client.run(*args)
        release.set()
        with pytest.raises(OSError):
            first.result()
    with pytest.raises(ApifyError):
        client.run(*args)
    with pytest.raises(ApifyError, match="预算不足"):
        client.run(adapter.actor, {"operation": "list_user_notes"}, 0.6)
    assert provider.starts == 1


def test_dynamic_tracking_and_replay(project, monkeypatch):
    from datetime import datetime, timezone
    from catering_watch import cli, db, matrix

    monkeypatch.setattr(cli, "settings", db.settings)
    brand = "新登记品牌"
    now = datetime.now(timezone.utc).timestamp()

    class FixtureBudget:
        execute = True
        steps = []

        def request(self, platform, payload, limit):
            self.steps.append((platform, payload))
            if payload["operation"] == "search_users":
                return {"id": "discovery", "fetch_id": -1}, [
                    {"user_id": "xhs-1", "nickname": "账号"}
                ]
            return {"id": "catalogue"}, [
                {
                    "note_id": "item",
                    "author_user_id": "xhs-1",
                    "title": "小红书作品",
                    "note_type": "video",
                    "publish_time": now,
                    "page_next_page_token": "cursor",
                    "page_has_more": True,
                }
            ]

    budget = FixtureBudget()
    assert tracking.search_accounts("xiaohongshu", brand, budget, 2, brand) == [
        ("xiaohongshu", "xhs-1")
    ]
    assert tracking.sync(brand, "xiaohongshu", "xhs-1", budget, 2, cli._persist)
    assert tracking.sync(brand, "xiaohongshu", "xhs-1", budget, 2, cli._persist)
    assert budget.steps[-1][1]["page_token"] == "cursor"
    row = next(row for row in matrix.read_report("coverage") if row["competitor"] == brand)
    assert row["xiaohongshu"] == "归属待核实" and row["owned_platform_count"] == 0
    tracking.register(brand, "xiaohongshu", "xhs-1", "https://example.org/proof")
    assert (
        next(row for row in matrix.read_report("coverage") if row["competitor"] == brand)[
            "owned_platform_count"
        ]
        == 1
    )
    assert db.db_status()["posts"] == 1


def test_repeated_evidence(project):
    from catering_watch import db

    a, b = [db.upsert_post({"platform": p, "post_id": "id"}) for p in ("douyin", "channels")]
    result = {"relation": "metadata_review_candidate", "score": 0.7, "reasons": ["caption"]}
    for _ in range(2):
        db.save_candidate_match(a, b, result, None, None)
    with connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM catering_watch.match_evidence WHERE project_key=%s",
                (project,),
            ).fetchone()["n"]
            == 3
        )


def test_account_scan_discovers_once_per_target(project, monkeypatch, tmp_path):
    from catering_watch import cli, db
    from typer.testing import CliRunner
    from datetime import datetime, timezone

    monkeypatch.setattr(cli, "settings", db.settings)
    monkeypatch.chdir(tmp_path)
    now = datetime.now(timezone.utc).timestamp()
    calls = []

    def request(self, platform, payload, limit):
        calls.append((platform, payload["operation"]))
        if payload["operation"] == "list_user_videos":
            return {"id": "source"}, [
                {
                    "aweme_id": str(i),
                    "author_sec_user_id": "source",
                    "author_name": "测试品牌",
                    "video_duration_ms": 20000,
                    "publish_time": now,
                }
                for i in range(10)
            ]
        return {"id": "discovery", "fetch_id": -1}, []

    monkeypatch.setattr(tracking.Budget, "request", request)
    result = CliRunner().invoke(
        cli.app,
        [
            "scan-url",
            "--url",
            "https://www.douyin.com/user/source",
            "--max-items",
            "10",
            "--max-cost",
            "0.8",
            "--execute",
        ],
    )
    assert result.exit_code == 0, result.exception
    assert calls == [
        ("douyin", "list_user_videos"),
        ("channels", "channelSearch"),
        ("xiaohongshu", "search_users"),
    ]
    assert db.db_status()["posts"] == 10


def test_aliases_require_direct_evidence(project):
    from catering_watch.db import upsert_post

    def insert(pid, ids):
        return upsert_post(
            {"platform": "channels", "post_id": pid, "metadata": {"raw_payload": ids}}
        )

    first = insert("exportId:z", {"exportId": "z"})
    assert insert("x", {"docID": "x", "exportId": "z"}) == first
    assert insert("x", {"docID": "x"}) == first
    assert insert("hashDocID:z", {"hashDocID": "z"}) != first


def test_long_running_actor_is_polled_without_restart(project, monkeypatch):
    clock = [0]

    class Slow(Provider):
        def status(self, provider):
            if clock[0] < 46:
                return {"id": provider, "status": "RUNNING"}
            return super().status(provider)

        def page(self, dataset, offset, limit):
            return []

    from types import SimpleNamespace

    monkeypatch.setattr(
        ingest,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ),
    )
    adapter = DouyinAdapter()
    provider = Slow()
    run, rows = ingest.DurableClient(adapter, provider).run(
        adapter.actor, {"operation": "search_users"}, 0.1
    )
    assert run["status"] == "SUCCEEDED" and not rows and provider.starts == 1 and clock[0] == 46
