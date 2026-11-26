import csv
import json
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from catering_watch import cli
from catering_watch.matrix import CATALOG, assemble_report, reports

BRAND = next(b for b in CATALOG if b["name"] == "侯宝贝")


def post(platform, **updates):
    return {
        "platform": platform,
        "platform_post_id": f"{platform}-1",
        "author_id": "account",
        "author_name": "侯宝贝",
        "verification_text": BRAND["entities"][0],
        "metadata": {"media_type": "video"},
        "title": "雨夜凌晨为南沙码头工人准备的第七百份姜汤",
        "body_text": None,
        "cover_phash": "1f6adebc44c56680",
        "duration_seconds": 23,
        "post_url": None,
        "published_at": None,
        **updates,
    }


def test_matrix_requires_direct_edges():
    posts = [
        post("douyin", cover_phash="123456789abc0000"),
        post("channels", cover_phash="123456789abc00ff"),
        post("xiaohongshu", cover_phash="123456789abcffff"),
    ]
    rows = reports("reposts", posts, [], [BRAND])
    assert [r["suspected_platform_count"] for r in rows] == [2, 3, 2]
    assert rows[0]["channels"][0]["reasons"]
    assert not rows[0]["xiaohongshu"]
    assert reports("coverage", posts, [], [BRAND])[0]["owned_platform_count"] == 3


@pytest.mark.parametrize(
    "updates",
    [
        {"verification_text": None},
        {"author_id": None},
        {"metadata": {"media_type": "image"}},
        {"duration_seconds": 42},
        {"cover_phash": None},
        {"cover_phash": "invalid"},
        {"title": "广州团餐配送"},
    ],
)
def test_exclude_insufficient_recut_and_third_party(updates):
    rows = reports("reposts", [post("douyin"), post("channels", **updates)], [], [BRAND])
    assert rows[0]["suspected_platform_count"] == 0


def test_generic_caption_and_cover():
    posts = [post(p, title="侯宝贝 广州团餐配送 员工餐 食堂承包") for p in ("douyin", "channels")]
    assert not reports("reposts", posts, [], [BRAND])[0]["channels"]


def test_empty_project_starts_with_no_observations():
    end = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)
    window = {"start": end.replace(month=5), "finish": end}
    for row in assemble_report("coverage", [], [], window):
        assert row["owned_platform_count"] == 0
        assert all(row[p] == "尚未查询" for p in ("douyin", "channels", "xiaohongshu"))


def test_failed_and_unsearched():
    run = {
        "platform": "douyin",
        "metadata": {"query": "侯宝贝"},
        "finished_at": None,
        "status": "FAILED",
    }
    row = reports("coverage", [], [run], [BRAND])[0]
    assert row["douyin"] == "查询失败"
    assert row["channels"] == "尚未查询"


def test_cli_dry_run_and_exports(tmp_path, monkeypatch):
    runner = CliRunner()
    assert runner.invoke(cli.app, ["--help"]).exit_code == 0
    result = runner.invoke(
        cli.app, ["scan-url", "--url", "https://www.douyin.com/video/7711674753035228403"]
    )
    assert result.exit_code == 0 and "xiaohongshu" in result.output and "channels" in result.output
    monkeypatch.setattr(
        cli,
        "read_report",
        lambda kind: reports(kind, [post("douyin"), post("channels")], [], [BRAND]),
    )
    for fmt in ("csv", "jsonl"):
        path = tmp_path / f"report.{fmt}"
        result = runner.invoke(
            cli.app, ["report", "--kind", "reposts", "--format", fmt, "--output", str(path)]
        )
        assert result.exit_code == 0
        with path.open(encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f)) if fmt == "csv" else [json.loads(line) for line in f]
        assert len(rows) == 2 and int(rows[0]["suspected_platform_count"]) == 2


def test_wheel_contains_runnable_cli_and_catalog(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from zipfile import ZipFile

    project = Path(__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-d", str(tmp_path)],
        cwd=project,
        check=True,
    )
    with ZipFile(next(tmp_path.glob("*.whl"))) as archive:
        archive.extractall(tmp_path)
    # Load only the wheel's package even when pytest is running in an editable install.
    script = (
        "import sys; sys.path.insert(0,sys.argv.pop(1)); "
        "from importlib.metadata import distributions; "
        "dist=next(distributions(path=[sys.path[0]])); "
        "next(e for e in dist.entry_points if e.name=='competitor-watch').load()()"
    )
    run = subprocess.run(
        [sys.executable, "-I", "-c", script, str(tmp_path), "--help"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "scan-url" in run.stdout


def test_cache_hit_reuses_posts_without_actor(monkeypatch):
    from catering_watch.adapters import ChannelsAdapter, DouyinAdapter
    from catering_watch.ingest import key_for

    monkeypatch.setenv("APIFY_BUILD", "test-build")
    adapter = ChannelsAdapter()
    expected = key_for(adapter.actor, adapter.search_payload("侯宝贝", 1), "test-build")

    def cached(fingerprint):
        assert fingerprint == expected
        return {"result_count": 1, "metadata": {"post_db_ids": [17]}}

    row = dict(post("channels"), id=17, cover_url=None)
    monkeypatch.setattr(cli, "ADAPTERS", {"douyin": DouyinAdapter, "channels": ChannelsAdapter})
    monkeypatch.setattr(cli, "find_cached_fetch", cached)
    monkeypatch.setattr(
        cli.tracking.Budget, "request", lambda *a: pytest.fail("unexpected Actor call")
    )
    monkeypatch.setattr(cli, "get_posts_by_ids", lambda ids: [row] if ids == [17] else [])
    result = cli._cross_search("douyin", "侯宝贝", 1, 0.1)
    assert result[0]["cache_hit"] and result[0]["post_id"] == row["platform_post_id"]


def test_search_observation_types():
    from types import SimpleNamespace

    general = SimpleNamespace(author_name="商家", title="盒饭", body=None)
    clue = SimpleNamespace(author_name="广东辉豪餐饮", title="盒饭", body=None)
    assert cli.search_outcome("侯宝贝", [], 0) == "empty"
    assert cli.search_outcome("侯宝贝", [], 1) == "sparse"
    assert cli.search_outcome("侯宝贝", [general], 1) == "no_relevant"
    assert cli.search_outcome("侯宝贝", [clue], 1) == "candidate"


def test_three_owned_platforms_can_publish_different_videos():
    posts = [
        post("douyin"),
        post("channels", title="学徒第一次给一百位客人包手工饺子"),
        post("xiaohongshu", title="周五客人票选出的红烧笋干配青鱼"),
    ]
    assert reports("coverage", posts, [], [BRAND])[0]["owned_platform_count"] == 3
    assert all(r["suspected_platform_count"] == 0 for r in reports("reposts", posts, [], [BRAND]))


def test_unknown_time_and_historical_seeds_do_not_add_coverage():
    start = datetime(2026, 5, 26, tzinfo=timezone.utc)
    finish = datetime(2026, 11, 26, tzinfo=timezone.utc)
    posts = [
        post("douyin", platform_post_id="7624150681927528881", published_at=start.replace(month=4)),
        post("channels"),
    ]
    rows = {
        r["competitor"]: r
        for r in assemble_report("coverage", posts, [], {"start": start, "finish": finish})
    }
    assert rows["侯宝贝"]["channels"] == "数据不足"
    assert rows["侯宝贝"]["owned_platform_count"] == 0
    assert rows["吉第餐饮"]["douyin"] == "仅有窗口外历史样本"
    assert len(rows["吉第餐饮"]["historical_seeds"]) == 1
