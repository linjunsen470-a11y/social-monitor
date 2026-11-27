import json
from dataclasses import replace

import pytest
from typer.testing import CliRunner

from catering_watch import apify, cli
from catering_watch.adapters import ChannelsAdapter, verification_text
from catering_watch.config import configured_token
from catering_watch.ingest import replay_fields
from catering_watch.research import Research, validate_review
from catering_watch.research_adapters import PublicDouyin, PublicRednote


@pytest.mark.parametrize(
    "row",
    [
        {"aweme_id": "d", "author_sec_user_id": "a", "author_enterprise_verify_reason": "企业"},
        {"aweme_id": "d", "author": {"sec_uid": "a", "enterprise_verify_reason": "企业"}},
        {"id": "d", "authorMeta": {"secUid": "a", "verificationReason": "企业"}},
    ],
)
def test_douyin_search_formats_retain_verified_author(row):
    adapter = PublicDouyin()
    item = adapter.normalize(replay_fields(adapter.prepare_row(row)))
    assert (item.post_id, item.author_id, verification_text(item.raw)) == ("d", "a", "企业")


@pytest.mark.parametrize(
    "identity",
    [
        {"note_id": "n", "author_user_id": "a", "author_enterprise_verify_reason": "企业"},
        {"id": "n", "user": {"user_id": "a"}, "enterprise_verify_reason": "企业"},
    ],
)
def test_rednote_preserves_evidence_fields(identity):
    adapter = PublicRednote()
    row = {
        **identity,
        "url": "https://example.com/n",
        "cover_image_url": "https://example.com/i",
        "video_duration_ms": 2000,
        "author_profile_url": "https://example.com/a",
    }
    item = adapter.normalize(replay_fields(adapter.prepare_row(row)))
    assert (item.url, item.author_url, item.cover_url) == (
        row["url"],
        row["author_profile_url"],
        row["cover_image_url"],
    )
    assert (item.post_id, item.author_id, item.duration_seconds, verification_text(item.raw)) == (
        "n",
        "a",
        2,
        "企业",
    )


@pytest.mark.parametrize(
    "notes",
    [
        {"selection": {}},
        {"terms": {" ": {}}},
        {"review": {}},
        {"reviews": {"品牌": {"conclusion": "已核对", "urls": ["无链接"]}}},
    ],
)
def test_invalid_review_fails_before_collection(notes, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "db_status", lambda: pytest.fail("invalid input reached database"))
    path = tmp_path / "review.json"
    path.write_text(json.dumps(notes))
    result = CliRunner().invoke(
        cli.app,
        ["research", "--query", "品牌", "--execute", "--max-cost", "0.1", "--review", str(path)],
    )
    assert result.exit_code == 2


def test_review_preserves_public_locator_and_drops_query_credentials():
    link = "https://channels.weixin.qq.com/finder-preview/pages/feed?eid=example"
    notes = validate_review(
        {
            "video_links": {"e": link + "&access_token=test"},
            "reviews": {
                "品牌": {
                    "conclusion": "已核对",
                    "urls": ["HTTPS://example.com/public?xsec_token=test"],
                }
            },
        }
    )
    assert notes["video_links"]["e"] == link
    assert notes["reviews"]["品牌"]["urls"] == ["https://example.com/public"]


@pytest.mark.parametrize(
    "change", [{"platform": "other"}, {"status": None}, {"operation": "other"}]
)
def test_invalid_replay_rejected_during_preflight(change):
    entry = {
        "platform": "channels",
        "operation": "videoSearch",
        "query": "q",
        "rows": [],
        "run_id": "r",
    }
    with pytest.raises(ValueError, match="第 2 条"):
        Research(None, None, [entry, {**entry, **change}])


def test_conflicting_detail_keeps_original_source():
    adapter = ChannelsAdapter()
    detail = {
        "platform": "channels",
        "operation": "videoDetail",
        "query": "e",
        "run_id": "r",
        "rows": [{"exportId": "e", "docID": "other", "username": "a"}],
    }
    worker = Research(None, None, [detail])
    source = adapter.normalize({"exportId": "e", "docID": "one"})
    item, run, checked = worker._enrich_channel(adapter, source, True)
    assert item is source and checked and run is None and item.author_id is None
    assert worker.incomplete and worker.checks[-1]["status"] == "详情标识冲突"


def test_token_priority_and_offline_doctor(monkeypatch):
    monkeypatch.setenv("APIFY_API_KEY", " fallback ")
    for token in (" ", "<YOUR_TOKEN>"):
        monkeypatch.setenv("APIFY_TOKEN", token)
        assert configured_token() == "fallback"
    monkeypatch.setenv("APIFY_TOKEN", " primary ")
    assert configured_token() == "primary"
    monkeypatch.setattr(cli, "settings", replace(cli.settings, database_url="", apify_token=""))
    assert CliRunner().invoke(cli.app, ["doctor"]).exit_code == 1
    monkeypatch.setattr(cli, "settings", replace(cli.settings, database_url="configured"))
    monkeypatch.setattr(cli, "db_status", dict)
    assert CliRunner().invoke(cli.app, ["doctor"]).exit_code == 0
    monkeypatch.setattr(apify, "settings", cli.settings)
    result = CliRunner().invoke(cli.app, ["doctor", "--check-apify"])
    assert result.exit_code == 1 and isinstance(result.exception, apify.ApifyError)
