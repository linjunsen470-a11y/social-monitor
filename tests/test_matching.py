import socket
from io import BytesIO

import httpx
import pytest
from PIL import Image

from catering_watch.matching import compare_posts, cover_feature, normalize_text


def test_normalize_chinese_text():
    assert normalize_text("员工餐配送！#广州团餐 https://x.test/a") == "员工餐配送"

def test_same_caption_is_candidate_not_proof():
    a={"title":"工厂员工餐配送","body_text":"每天准时送达，食堂承包"}
    b={"title":"工厂员工餐配送","body_text":"每天准时送达，食堂承包"}
    result=compare_posts(a,b)
    assert result["relation"] != "high_similarity_candidate"
    assert "不证明视频文件相同" in result["warning"]

def test_generic_topic_not_overstated():
    a={"title":"后厨现场现炒","body_text":"员工午餐"}
    b={"title":"园区绿化养护","body_text":"物业保洁服务"}
    assert compare_posts(a,b)["relation"] == "not_matched_by_metadata"


def test_rewritten_title_and_near_cover_only_enter_review_with_compatible_duration():
    a = {
        "title": "雨夜送出的第七百份姜汤",
        "cover_phash": "0123456789abcdef",
        "duration_seconds": 22,
    }
    b = {"title": "码头夜班工人的加餐", "cover_phash": "0123456789abcd10", "duration_seconds": 23}
    assert compare_posts(a, b)["relation"] == "metadata_review_candidate"
    assert (
        compare_posts(a, dict(b, cover_phash="0123456789abcc10"))["relation"]
        == "not_matched_by_metadata"
    )
    assert (
        compare_posts(a, dict(b, title=a["title"], duration_seconds=50))["relation"]
        == "not_matched_by_metadata"
    )


@pytest.mark.parametrize("public_target", [True, False])
def test_cover_validates_each_destination_and_preserves_tls_host(monkeypatch, public_target):
    resolved = []
    requested = []
    png = BytesIO()
    Image.new("RGB", (8, 8)).save(png, format="PNG")

    def resolve(host, port, **kwargs):
        resolved.append(host)
        ip = "1.1.1.1" if host == "first.test" or public_target else "192.0.2.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    def respond(request):
        host = request.headers["Host"]
        requested.append(host)
        assert request.url.host == "1.1.1.1"
        assert request.extensions["sni_hostname"] == host
        if host == "first.test":
            return httpx.Response(302, headers={"Location": "https://cover.test/p?sig=A%2Fb"})
        assert request.url.query == b"sig=A%2Fb"
        return httpx.Response(200, content=png.getvalue())

    original = httpx.Client
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(respond), **kw)
    )
    phash, status = cover_feature("https://first.test/image")
    assert resolved == ["first.test", "cover.test"]
    if public_target:
        assert status == "ok" and len(phash) == 16 and requested == resolved
    else:
        assert (phash, status) == (None, "blocked_url") and requested == ["first.test"]
