import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from decimal import Decimal
from uuid import uuid4

import pytest

from catering_watch import ingest, tracking
from catering_watch.adapters import ChannelsAdapter, DouyinAdapter
from catering_watch.apify import ApifyError
from catering_watch.db import connect, init_db


@pytest.mark.parametrize(
    "value",
    [
        1700000000,
        1700000000000,
        "2023-11-14T22:13:20Z",
        "2023-11-15 06:13:20",
        "2023-11-15T03:13:20+05:00",
    ],
)
def test_epoch(value):
    assert ingest.timestamp(value) == 1700000000


def test_distinct_channel_ids_and_credentials():
    adapter = ChannelsAdapter()
    assert adapter.normalize({"docID": "x"}).post_id != adapter.normalize({"exportId": "x"}).post_id
    assert ingest.clean({"auth_token": "secret", "url": "https://example.org/a?signature=s"}) == {
        "url": "https://example.org/a"
    }
    profile = "https://channels.weixin.qq.com/profile?channelId=stable"
    assert ingest.clean(profile + "&access_token=private") == profile
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
    init_db()
    with connect() as conn:
        conn.execute("INSERT INTO catering_watch.projects VALUES(%s,'fixture')", (key,))
    return key


class Provider:
    def __init__(self):
        self.starts = 0
        self.offsets = []
        self.fail = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

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
    cached, replay = client.run(*args)
    assert cached["cached"] and cached["command_cost_usd"] == 0 and replay == rows
    assert transport.starts == 1 and transport.offsets == [0, 100, 100]
    with connect() as db:
        ledger = db.execute(
            "SELECT * FROM catering_watch.cost_ledger WHERE project_key=%s", (project,)
        ).fetchall()
        reserved = db.execute(
            "SELECT reserved_usd FROM catering_watch.fetch_runs WHERE id=%s", (run["fetch_id"],)
        ).fetchone()["reserved_usd"]
    assert len(ledger) == 1 and ledger[0]["estimated"]
    assert ledger[0]["amount_usd"] < reserved == Decimal(".7")


def test_two_workers_can_read_the_same_page_and_settle_it_once(project, monkeypatch):
    entered = threading.Event()
    both_reading = threading.Barrier(2)

    class Concurrent(Provider):
        def page(self, dataset, offset, limit):
            if offset == 0:
                entered.set()
                both_reading.wait(timeout=5)
            return [{"aweme_id": str(i)} for i in range(offset, min(offset + limit, 103))]

    monkeypatch.setattr(ingest.time, "sleep", lambda _: None)
    adapter = DouyinAdapter()
    client = ingest.DurableClient(adapter, Concurrent())
    args = (adapter.actor, {"operation": "search_videos"}, ".55")
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(client.run, *args)
        assert entered.wait(5)
        second = executor.submit(client.run, *args)
        run, items = first.result(timeout=10)
        resumed, same_items = second.result(timeout=10)
    assert items == same_items == [{"aweme_id": str(i)} for i in range(103)]
    assert resumed["command_cost_usd"] == 0 and run["command_cost_usd"] == Decimal(".55")
    with connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM catering_watch.cost_ledger WHERE fetch_run_id=%s",
                (run["fetch_id"],),
            ).fetchone()["n"]
            == 1
        )


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
    with connect() as conn:
        state = conn.execute(
            """UPDATE catering_watch.fetch_runs SET started_at=now()-interval '2 days'
                             WHERE project_key=%s RETURNING status, reserved_usd""",
            (project,),
        ).fetchone()
        assert state == {"status": "RECONCILE", "reserved_usd": Decimal(".6")}
    with pytest.raises(ApifyError):
        client.run(*args)
    with pytest.raises(ApifyError, match="预算不足"):
        client.run(adapter.actor, {"operation": "list_user_notes"}, 0.6)
    assert provider.starts == 1


def test_dynamic_tracking_and_replay(project, monkeypatch):
    from datetime import datetime

    from catering_watch import cli, db, matrix

    monkeypatch.setattr(cli, "settings", db.settings)
    brand = "新登记品牌"
    now = datetime.now(UTC).timestamp()

    class FixtureBudget:
        execute = True

        def __init__(self):
            self.steps = []

        def request(self, platform, payload, limit):
            self.steps.append((platform, payload))
            if payload["operation"] == "search_users":
                return {"id": "discovery", "fetch_id": -1}, [
                    {"user_id": "xhs-1", "nickname": "新登记品牌账号"}
                ]
            historical = bool(payload.get("page_token"))
            return {"id": "catalogue"}, [
                {
                    "note_id": "older" if historical else "item",
                    "author_user_id": "xhs-1",
                    "title": "小红书作品",
                    "note_type": "video",
                    "publish_time": now,
                    "page_next_page_token": "cursor",
                    "page_has_more": not historical,
                }
            ]

    budget = FixtureBudget()
    (candidate,) = tracking.search_accounts("xiaohongshu", brand, budget, 2)
    assert candidate["id"] == "xhs-1" and candidate["nickname"] == "新登记品牌账号"
    with connect() as conn:
        assert not tracking.catalog_for(conn, [])
    assert tracking.sync(brand, "xiaohongshu", "xhs-1", budget, 1, cli._persist)
    assert tracking.sync(brand, "xiaohongshu", "xhs-1", budget, 1, cli._persist)
    assert "page_token" not in budget.steps[-1][1]
    assert tracking.sync(brand, "xiaohongshu", "xhs-1", budget, 1, cli._persist, backfill=True)
    assert budget.steps[-1][1]["page_token"] == "cursor"
    count = len(budget.steps)
    assert not tracking.sync(brand, "xiaohongshu", "xhs-1", budget, 1, cli._persist, backfill=True)
    assert len(budget.steps) == count
    row = next(row for row in matrix.read_report("coverage") if row["competitor"] == brand)
    assert row["xiaohongshu"] == "归属待核实" and row["owned_platform_count"] == 0
    tracking.register(brand, "xiaohongshu", "xhs-1", "https://example.org/proof")
    assert (
        next(row for row in matrix.read_report("coverage") if row["competitor"] == brand)[
            "owned_platform_count"
        ]
        == 1
    )
    assert db.db_status()["posts"] == 2


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
    from datetime import datetime

    from typer.testing import CliRunner

    from catering_watch import cli, db

    monkeypatch.setattr(cli, "settings", db.settings)
    monkeypatch.chdir(tmp_path)
    now = datetime.now(UTC).timestamp()
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
        if platform == "channels":
            return {"id": "discovery", "fetch_id": -1}, [
                {"channelId": "other", "creatorName": "路人账号"}
            ]
        return {"id": "discovery", "fetch_id": -1}, [{"user_id": "fan", "nickname": "测试品牌粉丝"}]

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
    with connect() as conn:
        catalog = tracking.catalog_for(conn, [])
    assert [a["id"] for a in catalog[0]["accounts"]] == ["source"]
    assert "测试品牌粉丝" in result.output


def test_aliases_require_direct_evidence(project):
    from catering_watch.db import upsert_post

    def insert(pid, ids):
        return upsert_post(
            {"platform": "channels", "post_id": pid, "metadata": {"raw_payload": ids}}
        )

    first = insert("exportId:z", {"exportId": "z"})
    assert insert("docID:x", {"docID": "x", "exportId": "z"}) == first
    assert insert("docID:x", {"docID": "x"}) == first
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


def test_budget_rejection_settlement_expiry_and_replay(project, monkeypatch):
    from catering_watch.apify import StartRejected

    class Billing(Provider):
        def start(self, actor, payload, cap, build):
            self.starts += 1
            if self.starts == 1:
                raise StartRejected("Actor start: HTTP 422 (invalid-input)")
            return {
                "id": str(self.starts),
                "status": "SUCCEEDED",
                "defaultDatasetId": "d",
                "usageTotalUsd": 0.007,
            }

        def page(self, dataset, offset, limit):
            return [{"aweme_id": "one"}]

    transport = Billing()
    monkeypatch.setattr(ingest, "ApifyClient", lambda: transport)
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.0600")
    budget = tracking.Budget(".0600", True)
    args = ("douyin", {"operation": "search_videos"}, 10)
    with pytest.raises(StartRejected):
        budget.request(*args)
    assert budget.remaining == Decimal(".0600")
    with connect() as conn:
        rejected = conn.execute(
            "SELECT * FROM catering_watch.fetch_runs WHERE project_key=%s", (project,)
        ).fetchone()

    assert (
        rejected["status"] == "FAILED" and rejected["finished_at"] and rejected["reserved_usd"] == 0
    )

    run, _ = budget.request(*args)
    assert budget.remaining == Decimal(".0530") and run["command_cost_usd"] == Decimal(".007")
    cached, _ = tracking.Budget(0, True).request(*args)
    assert cached["cached"] and cached["command_cost_usd"] == 0
    other, _ = tracking.Budget(".05", True).request("douyin", {"operation": "search_users"}, 10)
    assert other["command_cost_usd"] == Decimal(".007")
    with connect() as conn:
        ledger = conn.execute(
            "SELECT * FROM catering_watch.cost_ledger WHERE fetch_run_id=%s", (run["fetch_id"],)
        ).fetchone()
        assert not ledger["estimated"] and ledger["amount_usd"] == Decimal(".007")
        conn.execute(
            """UPDATE catering_watch.fetch_runs SET finished_at=now()-interval '25 hours',
                     started_at=now()-interval '26 hours' WHERE id=%s""",
            (run["fetch_id"],),
        )
    second, _ = budget.request(*args)
    assert not second["cached"] and budget.remaining == Decimal(".0460") and transport.starts == 4
    with connect() as conn:
        conn.execute("DELETE FROM catering_watch.fetch_runs WHERE id=%s", (run["fetch_id"],))
        assert not conn.execute(
            "SELECT 1 FROM catering_watch.dataset_pages WHERE fetch_id=%s", (run["fetch_id"],)
        ).fetchone()


def test_failed_requests_charge_only_their_own_reservation(project, monkeypatch):
    from dataclasses import replace

    with monkeypatch.context() as patch:
        patch.setattr(ingest, "settings", replace(ingest.settings, apify_token=""))
        budget = tracking.Budget(".20", True)
        with pytest.raises(ApifyError, match="未配置"):
            budget.request("douyin", {"operation": "search_users"}, 10)
        assert budget.remaining == Decimal(".20")

    class Uncertain(Provider):
        def start(self, *args):
            raise ApifyError("启动结果未知")

    monkeypatch.setattr(ingest, "ApifyClient", Uncertain)
    args = ("douyin", {"operation": "search_videos"}, 10)
    with pytest.raises(ApifyError, match="启动结果未知"):
        budget.request(*args)
    assert budget.remaining == Decimal(".1500")
    with pytest.raises(ApifyError, match="待核对"):
        budget.request(*args)
    assert budget.remaining == Decimal(".1500")

    class Aborted(Provider):
        def start(self, *args):
            return {"id": "stopped", "status": "ABORTED", "usageTotalUsd": 0.006}

    monkeypatch.setattr(ingest, "ApifyClient", Aborted)
    with pytest.raises(ApifyError, match="ABORTED"):
        budget.request("douyin", {"operation": "search_users"}, 10)
    assert budget.remaining == Decimal(".1440")


def test_signed_cover_survives_replay_and_persistence(project, monkeypatch):
    from catering_watch import cli, db

    signed = "https://cdn.example/image?x-signature=A%2fb%2BZ&x-expires=123&lk3s=k"

    class Signed(Provider):
        def page(self, dataset, offset, limit):
            return [
                {
                    "aweme_id": "item",
                    "cover_image_url": signed + "&access_token=secret#private",
                    "share_url": "https://www.douyin.com/video/item?signature=private",
                }
            ]

    monkeypatch.setattr(ingest.time, "sleep", lambda _: None)
    monkeypatch.setattr(cli, "settings", db.settings)
    received = []

    def fetch_cover(url, platform):
        received.append((url, platform))
        return "0123456789abcdef", "ok"

    monkeypatch.setattr(cli, "cover_feature", fetch_cover)
    adapter = DouyinAdapter()
    client = ingest.DurableClient(adapter, Signed())
    for cached in (False, True):
        run, rows = client.run(adapter.actor, {"operation": "search_videos"}, ".05")
        assert run["cached"] is cached and rows[0]["cover_image_url"] == signed
        cli._persist(adapter.normalize(rows[0]))
    post = db.stored_post("douyin", "item")
    assert post["cover_url"] == signed == post["metadata"]["raw_payload"]["cover_image_url"]
    assert post["post_url"] == "https://www.douyin.com/video/item"
    assert post["cover_phash"] and received == [(signed, "douyin")]


def test_channels_account_aliases_and_full_page(project, monkeypatch):
    from catering_watch import cli, db, matrix

    monkeypatch.setattr(cli, "settings", db.settings)
    tracking.register("测试品牌", "channels", "finder", "https://example.org/proof")

    class Pages:
        execute = True

        def request(self, platform, payload, limit):
            if payload["operation"] == "channelSearch":
                return {"id": "search", "fetch_id": -1}, [
                    {"channelId": "channel", "finderUserName": "finder", "title": "测试品牌"}
                ]
            return {"id": "videos"}, [
                {"hashDocID": "shared", "docID": "doc", "channelId": "channel"},
                {"exportId": "shared", "finderUserName": "finder"},
            ]

    budget = Pages()
    (candidate,) = tracking.search_accounts("channels", "测试品牌", budget, 1)
    assert candidate["id"] == "channel"
    ids = tracking.sync("测试品牌", "channels", "channel", budget, 1, cli._persist)
    assert len(set(ids)) == 2
    assert db.stored_post("channels", "hashDocID:shared", {"hashDocID": "shared"})["id"] == ids[0]
    with connect() as conn:
        (brand,) = tracking.catalog_for(conn, [])
    assert all(
        matrix.ownership(brand, p) == "https://example.org/proof" for p in db.get_posts_by_ids(ids)
    )
    assert cli._persist(ChannelsAdapter().normalize({"docID": "shared"})) not in ids


def test_channel_account_promotion_preserves_post_and_nonempty_fields(project):
    from catering_watch import db

    account = db.upsert_account(
        "channels",
        "user",
        url="https://example.com/u",
        bio="profile",
        source_metadata={"username": "user"},
    )
    post = db.upsert_post(
        {
            "platform": "channels",
            "post_id": "docID:p",
            "account_id": account,
            "url": "https://example.com/p",
            "cover_url": "https://example.com/i",
        }
    )
    assert (
        db.upsert_account(
            "channels",
            "channel",
            url="",
            bio="",
            verification="企业",
            source_metadata={"channelId": "channel", "username": "user"},
        )
        == account
    )
    assert (
        db.upsert_post({"platform": "channels", "post_id": "docID:p", "url": "", "cover_url": ""})
        == post
    )
    saved = db.stored_post("channels", "docID:p")
    assert (saved["author_id"], saved["verification_text"], saved["author_url"]) == (
        "channel",
        "企业",
        "https://example.com/u",
    )
    assert (saved["post_url"], saved["cover_url"]) == (
        "https://example.com/p",
        "https://example.com/i",
    )
    tracking.register("品牌", "channels", "user", "https://example.com/proof")
    db.upsert_account(
        "channels", "other", source_metadata={"channelId": "other", "username": "user"}
    )
    with connect() as conn:
        (brand,) = tracking.catalog_for(conn, [])
    assert brand["accounts"][0]["ids"] == []


def test_partial_source_page_keeps_bookmark_until_complete(project, monkeypatch):
    from catering_watch import cli, db

    monkeypatch.setattr(cli, "settings", db.settings)

    class Partial:
        execute = True

        def request(self, platform, payload, limit):
            assert "page_token" not in payload
            return {"id": "catalogue"}, [
                {"note_id": str(i), "page_item_count": 3, "page_next_page_token": "next"}
                for i in range(min(3, limit))
            ]

    args = ("品牌", "xiaohongshu", "account", Partial())
    with pytest.raises(ValueError, match="游标保留"):
        tracking.sync(*args, 2, cli._persist, backfill=True)
    assert db.db_status()["posts"] == 2
    assert len(tracking.sync(*args, 3, cli._persist, backfill=True)) == 3
    with connect() as conn:
        state = conn.execute(
            "SELECT next_cursor FROM catering_watch.tracked_accounts WHERE project_key=%s",
            (project,),
        ).fetchone()
    assert state["next_cursor"] == "next"
    assert db.stored_post("xiaohongshu", "0")["author_id"] == "account"


def test_verified_peer_search_uses_actor_fields(project, monkeypatch, tmp_path):
    from datetime import datetime

    from typer.testing import CliRunner

    from catering_watch import cli, db

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "settings", db.settings)
    now = datetime.now(UTC).timestamp()
    calls = []

    def request(self, platform, payload, limit):
        calls.append(payload)
        if payload["operation"] == "list_user_videos":
            return {"id": "source"}, [
                {
                    "aweme_id": "source",
                    "author_name": "侯宝贝",
                    "author_sec_user_id": "brand",
                    "publish_time": now,
                }
            ]
        if payload["operation"] == "channelSearch":
            return {"id": "peers", "fetch_id": -1}, [
                {
                    "username": "finder",
                    "channelId": "channel",
                    "nickname": "账号",
                    "verifiedEntity": "广东辉豪餐饮管理有限公司",
                    "fansCount": 32,
                },
                {"username": "unrelated", "nickname": "侯宝贝粉丝"},
            ]
        if payload["operation"] == "channelVideos":
            assert payload["channels"] == ["channel"]
            return {"id": "sync"}, [
                {
                    "videoId": "1",
                    "username": "finder",
                    "createTime": now,
                    "description": "作品",
                    "media": {"videoPlayLen": 25},
                }
            ]
        return {"id": "empty", "fetch_id": -1}, []

    monkeypatch.setattr(tracking.Budget, "request", request)
    result = CliRunner().invoke(
        cli.app,
        [
            "scan-url",
            "--url",
            "https://www.douyin.com/user/brand",
            "--execute",
            "--max-cost",
            "0.8",
        ],
    )
    assert result.exit_code == 0, result.exception
    assert [p["channels"] for p in calls if p["operation"] == "channelVideos"] == [["channel"]]
    row = db.stored_post("channels", "videoId:1")
    assert row["title"] == "作品" and row["duration_seconds"] == 25
    assert row["author_id"] == "channel" and row["verification_text"] == "广东辉豪餐饮管理有限公司"


def test_feed_media_keeps_only_the_signed_wechat_cover():
    raw = {
        "videoId": "x",
        "media": {
            "url": "https://example.org/video",
            "decodeKey": "private",
            "coverUrl": "https://wxapp.tc.qq.com/251/20304/stodownload?encfilekey=key",
            "coverUrlToken": "&token=cover%2Bsignature",
            "videoPlayLen": 20,
        },
    }
    stored = ingest.replay_fields(raw)
    assert stored == {
        "videoId": "x",
        "image": raw["media"]["coverUrl"] + "&token=cover%2Bsignature",
        "duration": 20,
    }
    assert (
        ChannelsAdapter().normalize(stored).cover_url == ChannelsAdapter().normalize(raw).cover_url
    )


def test_new_posts_after_pinned_item_do_not_rewind_backfill(project, monkeypatch):
    from catering_watch import cli, db

    monkeypatch.setattr(cli, "settings", db.settings)
    args = ("品牌", "douyin", "account")
    tracking.register(*args)
    with connect() as conn:
        conn.execute(
            "UPDATE catering_watch.tracked_accounts SET next_cursor='historical' WHERE project_key=%s",
            (project,),
        )
    for pid in ("pinned", "seen"):
        db.upsert_post({"platform": "douyin", "post_id": pid})

    calls = []

    class Feed:
        execute = True

        def request(self, platform, payload, limit):
            cursor = payload.get("page_token")
            calls.append(cursor)
            rows = {
                None: [("pinned", True), ("new-1", False)],
                "second": [("new-2", False), ("seen", False)],
            }[cursor]
            return {"id": "feed"}, [
                {
                    "aweme_id": pid,
                    "is_pinned": pinned,
                    "page_next_page_token": "third" if cursor else "second",
                }
                for pid, pinned in rows
            ]

    tracking.sync(*args, Feed(), 6, cli._persist)
    assert calls == [None, "second"] and db.db_status()["posts"] == 4
    with connect() as conn:
        row = conn.execute(
            "SELECT next_cursor FROM catering_watch.tracked_accounts WHERE project_key=%s",
            (project,),
        ).fetchone()
    assert row["next_cursor"] == "historical"


def test_real_scan_cache_avoids_reprocessing_persisted_posts(project, monkeypatch):
    from catering_watch import cli, db

    class Search(Provider):
        def start(self, actor, payload, cap, build):
            self.starts += 1
            return {
                "id": actor,
                "status": "SUCCEEDED",
                "defaultDatasetId": actor,
                "usageTotalUsd": 0.005,
            }

        def page(self, dataset, offset, limit):
            return [{"note_id": "n"}] if "xhs" in dataset else [{"docID": "c"}]

    provider = Search()
    monkeypatch.setattr(ingest, "ApifyClient", lambda: provider)
    monkeypatch.setattr(cli, "settings", db.settings)
    args = ("douyin", "品牌", 2, tracking.Budget(0.2, True))
    rows, ok = cli._cross_search(*args)
    assert ok and len(rows) == 2 and not any(r["cache_hit"] for r in rows)
    monkeypatch.setattr(
        cli, "_persist", lambda *a, **kw: pytest.fail("processed a cached post twice")
    )
    cached, ok = cli._cross_search(*args)
    assert ok
    assert len(cached) == 2 and all(r["cache_hit"] for r in cached)
    assert provider.starts == 2


def test_research_real_cli_replay_and_offline_export(project, monkeypatch, tmp_path):
    import json

    from openpyxl import load_workbook
    from typer.testing import CliRunner

    from catering_watch import cli, db, research

    for module in (research, cli):
        monkeypatch.setattr(module, "settings", db.settings)
    monkeypatch.setattr(tracking.Budget, "request", lambda *a, **kw: pytest.fail("paid call"))
    monkeypatch.setattr(cli, "_ensure_cover", lambda *a, **kw: pytest.fail("network call"))
    brand = "侯宝贝"
    entity = next(b["entities"][0] for b in research.CATALOG if b["name"] == brand)
    archive = [
        (
            "channels",
            "videoSearch",
            "广州食堂承包",
            [{"docID": "doc", "exportId": "export/x", "title": "=menu"}],
        ),
        (
            "channels",
            "videoDetail",
            "export/x",
            [
                {
                    "videoId": "vid",
                    "username": "wx-id",
                    "nickname": brand,
                    "title": "",
                    "verifiedEntity": entity,
                }
            ],
        ),
        (
            "douyin",
            "public_post_search",
            brand,
            [{"aweme_id": "d", "desc": "试餐", "author": {"nickname": brand, "sec_uid": "dy-id"}}],
        ),
        (
            "xiaohongshu",
            "public_post_search",
            brand,
            [
                {
                    "id": "n",
                    "user": {"userid": "xhs-id", "nickname": brand},
                    "type": "normal",
                    "desc": "餐标",
                    "author_enterprise_verify_reason": entity,
                }
            ],
        ),
    ]
    records = [
        {"platform": p, "operation": o, "query": q, "rows": rows, "run_id": f"run-{i}"}
        for i, (p, o, q, rows) in enumerate(archive)
    ]
    manifest = tmp_path / "inputs.json"
    output = tmp_path / "out.xlsx"
    review = tmp_path / "review.json"
    review.write_text(
        json.dumps(
            {
                "selection": {"export/x": brand},
                "video_links": {"export/x": "https://example.com/video?access_token=private"},
                "reviews": {
                    brand: {
                        "conclusion": "已核对",
                        "urls": ["https://example.com/proof?xsec_token=private"],
                    }
                },
            }
        )
    )
    args = [
        "research",
        "--query",
        "广州食堂承包",
        "--replay",
        str(manifest),
        "--execute",
        "--output",
        str(output),
    ]
    runner = CliRunner()
    for complete in (True, False):
        manifest.write_text(json.dumps(records if complete else records[:-1]))
        result = runner.invoke(cli.app, [*args, "--review", str(review)] if complete else args)
        assert result.exit_code == (0 if complete else 1), (result.output, result.exception)
        rid = result.output.split("调研 ID ")[1].splitlines()[0]
        book = load_workbook(output)
        wx = list(book["微信视频作品"].values)[1]
        assert wx[0:3] == (brand, "channels", brand) and "wx-id" in wx
        assert "=menu" in wx and all(cell.data_type != "f" for cell in book["微信视频作品"][2])
        assert len(list(book["跨平台作品证据"].values)) == (3 if complete else 2)
        status = list(book["竞品三端核查"].values)[1][3]
        assert status == ("有候选；有自营依据" if complete else "缺少回放记录")
        if complete:
            assert entity in wx and "https://example.com/video" in wx
            with connect() as conn:
                evidence = conn.execute(
                    "SELECT evidence FROM catering_watch.research_reports WHERE id=%s", (rid,)
                ).fetchone()["evidence"]
            assert "private" not in json.dumps(evidence)
            assert evidence["peers"][-1]["ownership_evidence"] == entity
            assert db.stored_post("douyin", "d")["metadata"]["media_type"] == "video"
            assert db.stored_post("xiaohongshu", "n")["metadata"]["media_type"] == "image"
            assert db.stored_post("channels", "docID:doc")["verification_text"] == entity
        copy = tmp_path / "export.xlsx"
        assert (
            runner.invoke(cli.app, ["research-report", rid, "--output", str(copy)]).exit_code == 0
        )
        assert [list(s.values) for s in book] == [list(s.values) for s in load_workbook(copy)]
    with connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) n FROM catering_watch.posts WHERE project_key=%s", (project,)
            ).fetchone()["n"]
            == 3
        )


def test_research_online_budget_and_actor_protocol(project, monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from catering_watch import cli, db, research

    starts = []

    class Transport(Provider):
        def start(self, actor, payload, cap, build):
            starts.append((actor, payload, cap))
            assert cap > 0
            return {
                "id": actor,
                "defaultDatasetId": actor,
                "usageTotalUsd": 0.005,
                "status": "SUCCEEDED",
            }

        def page(self, dataset, offset, limit):
            if "wechat" in dataset:
                return [{"exportId": "export/x", "creatorName": "本地餐饮", "username": "wx"}]
            if "douyin" in dataset:
                return [{"aweme_id": "d", "author": {"nickname": "dy", "sec_uid": "stable"}}]
            return [{"id": "n", "user": {"userid": "x", "nickname": "red"}, "desc": "团餐菜单"}]

    for module in (cli, research):
        monkeypatch.setattr(module, "settings", db.settings)
    monkeypatch.setattr(ingest, "ApifyClient", Transport)
    args = [
        "research",
        "--query",
        "广州团餐",
        "--execute",
        "--max-cost",
        "0.2",
        "--max-items",
        "2",
        "--output",
        str(tmp_path / "a.xlsx"),
    ]
    for _ in range(2):
        r = CliRunner().invoke(cli.app, args)
        assert r.exit_code == 0, (r.output, r.exception)
    assert len(starts) == 3 and starts[0][1]["operation"] == "videoSearch"
    assert starts[1][1] == {"keyword": "本地餐饮", "maxItems": 3}
    assert starts[2][1] == {"query": "本地餐饮", "maxItems": 3}
    with connect() as conn:
        red = conn.execute(
            "SELECT a.current_name,p.title FROM catering_watch.posts p JOIN catering_watch.accounts a ON a.id=p.account_id WHERE p.project_key=%s AND p.platform='xiaohongshu'",
            (project,),
        ).fetchone()
    assert red == {"current_name": "red", "title": "团餐菜单"}


def test_research_alias_conflict_saves_other_evidence(project, monkeypatch, tmp_path):
    import json

    from openpyxl import load_workbook
    from typer.testing import CliRunner

    from catering_watch import cli, db, research

    for module in (cli, research):
        monkeypatch.setattr(module, "settings", db.settings)
    for ids in ({"docID": "first"}, {"exportId": "second"}):
        cli._persist(ChannelsAdapter().normalize(ids), fetch_cover=False)
    rows = [
        {"docID": "first", "exportId": "second", "username": "conflict-only", "nickname": "待核对"},
        {"docID": "ok", "username": "good", "nickname": "品牌"},
    ]
    archive = [
        {
            "platform": "channels",
            "operation": "videoSearch",
            "query": "q",
            "rows": rows,
            "run_id": "source",
        }
    ]
    for platform in ("douyin", "xiaohongshu"):
        archive.append(
            {
                "platform": platform,
                "operation": "public_post_search",
                "query": "品牌",
                "rows": [],
                "run_id": platform,
            }
        )
    path = tmp_path / "archive.json"
    path.write_text(json.dumps(archive))
    output = tmp_path / "results.xlsx"
    result = CliRunner().invoke(
        cli.app,
        ["research", "--query", "q", "--execute", "--replay", str(path), "--output", str(output)],
    )
    assert result.exit_code == 1 and "调研 ID" in result.output
    book = load_workbook(output)
    assert [r[4] for r in list(book["微信视频作品"].values)[1:]] == ["docID:ok"]
    assert any("别名冲突" in r[3] for r in list(book["检索记录"].values)[1:])
    assert db.db_status()["accounts"] == 1
