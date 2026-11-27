from catering_watch.adapters import ChannelsAdapter, DouyinAdapter, XiaohongshuAdapter


def test_douyin_normalize():
    item = DouyinAdapter().normalize({
        "aweme_id":"1234567890123456789","description":"员工餐","publish_time":123,
        "cover_image_url":"https://example.com/c.jpg","author_name":"品牌"
    })
    assert item.post_id == "1234567890123456789"
    assert item.title == "员工餐"

def test_channels_normalize():
    item = ChannelsAdapter().normalize({
        "docID":"opaque-id","title":"食堂承包","pubTime":123,"duration":"01:23",
        "creatorName":"账号","image":"https://example.com/c.jpg"
    })
    assert item.post_id == "docID:opaque-id"
    assert item.duration_seconds == 83

def test_xhs_normalize():
    item = XiaohongshuAdapter().normalize({
        "note_id":"abc123","title":"团餐","summary":"员工餐","publish_time":123
    })
    assert item.post_id == "abc123"
    assert item.body == "员工餐"

def test_track_profile_uses_account_path_segment(monkeypatch):
    from typer.testing import CliRunner

    from catering_watch import cli

    seen = []
    monkeypatch.setattr(
        cli.tracking, "register", lambda brand, platform, uid, evidence: seen.append(uid)
    )
    for platform, url in [
        ("douyin", "https://www.douyin.com/user/stable/video"),
        ("xiaohongshu", "https://www.xiaohongshu.com/user/profile/stable/notes"),
        (
            "channels",
            "https://channels.weixin.qq.com/profile?channelId=stable&finderUserName=alias",
        ),
    ]:
        result = CliRunner().invoke(
            cli.app, ["track-account", "品牌", platform, url, "--kind", "url", "--execute"]
        )
        assert result.exit_code == 0, result.exception
    assert seen == ["stable"] * 3


def test_track_douyin_mobile_share_redirect(monkeypatch):
    import httpx
    from typer.testing import CliRunner

    from catering_watch import cli

    short = "https://v.douyin.com/abc/"
    profile = "https://www.iesdouyin.com/share/user/stable?from=share"

    def handle(request):
        if str(request.url) == short:
            return httpx.Response(302, headers={"Location": profile})
        assert str(request.url) == profile
        return httpx.Response(200)

    client_type = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: client_type(transport=httpx.MockTransport(handle), **kw)
    )
    seen = []
    monkeypatch.setattr(cli.tracking, "register", lambda *args: seen.append(args))
    result = CliRunner().invoke(
        cli.app, ["track-account", "品牌", "douyin", short, "--kind", "url", "--execute"]
    )
    assert result.exit_code == 0, result.exception
    assert seen == [("品牌", "douyin", "stable", None)]


def test_channels_preview_preserves_only_public_locator():
    from urllib.parse import parse_qs, urlsplit

    from catering_watch.adapters import is_profile_reference
    from catering_watch.ingest import clean

    adapter = ChannelsAdapter()
    url = adapter.normalize({"exportId": "export/abc+123"}).url
    assert not is_profile_reference("channels", url)
    assert adapter.reference_payload(url)["videos"] == ["export/abc+123"]
    assert parse_qs(urlsplit(clean(url + "&access_token=private")).query) == {
        "eid": ["export/abc+123"]
    }
    assert (
        clean("https://example.org/finder-preview/pages/feed?eid=x")
        == "https://example.org/finder-preview/pages/feed"
    )


def test_scan_exits_nonzero_without_overwriting_last_good_export(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from catering_watch import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "db_status", dict)

    def unavailable(*args):
        raise RuntimeError("DB offline")

    monkeypatch.setattr(cli, "find_cached_fetch", unavailable)
    result = CliRunner().invoke(
        cli.app,
        ["scan", "--platform", "channels", "--query", "团餐", "--execute", "--max-cost", "0.1"],
    )
    assert result.exit_code == 1 and "所有目标平台均未完成" in result.output
    assert not (tmp_path / "outputs").exists()
