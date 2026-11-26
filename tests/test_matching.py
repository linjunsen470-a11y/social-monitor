from catering_watch.matching import normalize_text, compare_posts

def test_normalize_chinese_text():
    assert normalize_text("员工餐配送！#广州团餐 https://x.test/a") == "员工餐配送"

def test_same_caption_is_candidate_not_proof():
    a={"title":"工厂员工餐配送","body_text":"每天准时送达，食堂承包"}
    b={"title":"工厂员工餐配送","body_text":"每天准时送达，食堂承包"}
    result=compare_posts(a,b)
    assert result["relation"] == "high_similarity_candidate"
    assert "不证明视频文件相同" in result["warning"]

def test_generic_topic_not_overstated():
    a={"title":"后厨现场现炒","body_text":"员工午餐"}
    b={"title":"园区绿化养护","body_text":"物业保洁服务"}
    assert compare_posts(a,b)["relation"] == "not_matched_by_metadata"
