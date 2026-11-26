import re
import unicodedata

from rapidfuzz.fuzz import ratio

from . import __version__

COVER_DISTANCE_LIMIT = 8


def normalize_text(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"#[\w\u4e00-\u9fff]+", " ", value)
    value = re.sub(r"[^\w\u4e00-\u9fff]+", " ", value)
    return " ".join(value.split())


GENERIC = re.compile(
    r"有限公司|餐饮管理|团餐配送|团餐|食堂承包|广州|广东|员工餐|餐饮|配送|工作餐|企业|食堂|美食|后厨|厨房|现场|每天|新鲜|现炒|菜品|定制|\s+"
)


def distinctive(value, brand):
    text = normalize_text(value)
    for term in sorted(
        [brand.get("name", ""), *brand.get("aliases", []), *brand.get("entities", [])],
        key=len,
        reverse=True,
    ):
        text = text.replace(normalize_text(term), "")
    return GENERIC.sub("", text)


def cover_distance(source, target):
    hashes = [p.get("cover_phash") for p in (source, target)]
    if not all(re.fullmatch(r"[0-9a-fA-F]{16}", h or "") for h in hashes):
        return None
    return (int(hashes[0], 16) ^ int(hashes[1], 16)).bit_count()


def durations_compatible(source, target):
    a, b = [p.get("duration_seconds") for p in (source, target)]
    return not (a and b) or abs(float(a) - float(b)) <= max(2, 0.05 * min(float(a), float(b)))


def repost_reasons(source, target, brand):
    distance = cover_distance(source, target)
    if (
        distance is None
        or distance > COVER_DISTANCE_LIMIT
        or not durations_compatible(source, target)
    ):
        return []
    left, right = (
        [distinctive(p.get(k), brand) for k in ("title", "body_text")] for p in (source, target)
    )
    if not any(min(len(x), len(y)) >= 8 and ratio(x, y) >= 88 for x in left for y in right):
        return []
    reasons = [
        f"封面指纹距离 {distance}/64",
        "独特文案高度相似",
        "时长接近"
        if source.get("duration_seconds") and target.get("duration_seconds")
        else "时长缺失",
    ]
    if source.get("published_at") and target.get("published_at"):
        gap = (target["published_at"] - source["published_at"]).total_seconds() / 3600
        reasons.append(f"目标作品发布时间相对源作品 {gap:+.1f} 小时")
    return reasons


def compare_posts(a, b, brand=None):
    brand = brand or {}
    reasons = repost_reasons(a, b, brand)
    relation = "high_similarity_candidate" if reasons else "not_matched_by_metadata"

    def grams(post):
        texts = {distinctive(post.get(k), brand) for k in ("title", "body", "body_text")}
        return {t[i : i + 2] for t in texts if len(t) >= 4 for i in range(len(t) - 1)}

    left, right = grams(a), grams(b)
    score = len(left & right) / max(1, len(left | right))
    if not reasons and durations_compatible(a, b):
        distance = cover_distance(a, b)
        if score >= 0.5:
            reasons.append("独特文案有重叠，整条重发证据不足")
        if distance is not None and distance <= COVER_DISTANCE_LIMIT:
            reasons.append(f"封面指纹距离 {distance}/64，独立文本证据不足")
        if reasons:
            relation = "metadata_review_candidate"
    return {
        "relation": relation,
        "score": round(score, 3),
        "reasons": reasons,
        "warning": "仅基于元数据形成候选，不证明视频文件相同。",
    }


def cover_feature(url, platform=None):
    from io import BytesIO

    import httpx
    import imagehash
    from PIL import Image

    if not url:
        return None, "missing"
    try:
        headers = {"User-Agent": f"catering-watch/{__version__}"}
        referer = {
            "douyin": "https://www.douyin.com/",
            "channels": "https://channels.weixin.qq.com/",
            "xiaohongshu": "https://www.xiaohongshu.com/",
        }.get(platform)
        if referer:
            headers["Referer"] = referer
        with httpx.stream(
            "GET", url, headers=headers, timeout=12, follow_redirects=True
        ) as response:
            if response.is_error:
                return None, f"http_{response.status_code}"
            data = BytesIO()
            for chunk in response.iter_bytes():
                data.write(chunk)
                if data.tell() > 8_000_000:
                    return None, "too_large"
        data.seek(0)
        with Image.open(data) as image:
            image.thumbnail((512, 512))
            return str(imagehash.phash(image.convert("RGB"))), "ok"
    except httpx.RequestError:
        return None, "network_error"
    except (OSError, ValueError, Image.DecompressionBombError):
        return None, "decode_error"
