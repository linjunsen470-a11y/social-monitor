from catering_watch.adapters import DouyinAdapter, ChannelsAdapter, XiaohongshuAdapter, detect_platform

def test_douyin_normalize():
    item = DouyinAdapter(client=object()).normalize({
        "aweme_id":"1234567890123456789","description":"员工餐","publish_time":123,
        "cover_image_url":"https://example.com/c.jpg","author_name":"品牌"
    })
    assert item.post_id == "1234567890123456789"
    assert item.title == "员工餐"

def test_channels_normalize():
    item = ChannelsAdapter(client=object()).normalize({
        "docID":"opaque-id","title":"食堂承包","pubTime":123,"duration":"01:23",
        "creatorName":"账号","image":"https://example.com/c.jpg"
    })
    assert item.post_id == "opaque-id"
    assert item.duration_seconds == 83

def test_xhs_normalize():
    item = XiaohongshuAdapter(client=object()).normalize({
        "note_id":"abc123","title":"团餐","summary":"员工餐","publish_time":123
    })
    assert item.post_id == "abc123"
    assert item.body == "员工餐"

def test_platform_detection():
    assert detect_platform("https://www.douyin.com/video/123456789012") == "douyin"
    assert detect_platform("https://www.xiaohongshu.com/explore/abc") == "xiaohongshu"


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
