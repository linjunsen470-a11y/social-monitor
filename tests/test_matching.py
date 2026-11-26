from catering_watch.matching import normalize_text, compare_posts

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
