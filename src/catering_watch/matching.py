import re, unicodedata
from rapidfuzz.fuzz import token_set_ratio, ratio

def normalize_text(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"#[\w\u4e00-\u9fff]+", " ", value)
    value = re.sub(r"[^\w\u4e00-\u9fff]+", " ", value)
    return " ".join(value.split())

def compare_posts(a: dict, b: dict) -> dict:
    at, bt = normalize_text(a.get("title")), normalize_text(b.get("title"))
    ab = normalize_text(a.get("body_text") or a.get("body"))
    bb = normalize_text(b.get("body_text") or b.get("body"))
    title_score = token_set_ratio(at, bt) / 100 if at and bt else 0.0
    body_score = token_set_ratio(ab, bb) / 100 if ab and bb else 0.0
    text_score = max(title_score, body_score,
        ratio(at + " " + ab, bt + " " + bb) / 100 if (at or ab) and (bt or bb) else 0.0)
    reasons = []
    if title_score >= .75: reasons.append("title_overlap")
    if body_score >= .70: reasons.append("body_overlap")
    cover_score = None
    if a.get("cover_phash") and b.get("cover_phash"):
        try:
            distance = (int(a["cover_phash"],16) ^ int(b["cover_phash"],16)).bit_count()
            cover_score = max(0.0, 1 - distance / 64)
            if distance <= 8: reasons.append("cover_phash_close")
        except ValueError: pass
    if title_score >= .80 and body_score >= .80 and (cover_score is None or cover_score >= .80):
        relation = "high_similarity_candidate"
    elif len(reasons) >= 2 and text_score >= .65:
        relation = "suspected_recut_or_reused_material"
    elif text_score >= .45 or (cover_score is not None and cover_score >= .85):
        relation = "similar_theme_or_weak_candidate"
    else:
        relation = "not_matched_by_metadata"
    return {"relation":relation,"score":round(text_score,3),"title_score":round(title_score,3),
            "body_score":round(body_score,3),"cover_score":round(cover_score,3) if cover_score is not None else None,
            "reasons":reasons,
            "warning":"仅基于封面/标题/正文元数据形成候选，不证明视频文件相同。"}

def cover_phash(url: str | None) -> str | None:
    """Fetch a public cover image only; never fetch the video media."""
    if not url: return None
    try:
        import httpx, imagehash
        from io import BytesIO
        from PIL import Image
        response = httpx.get(url, timeout=12, follow_redirects=True,
                             headers={"User-Agent":"Mozilla/5.0"})
        response.raise_for_status()
        if len(response.content) > 8_000_000: return None
        image = Image.open(BytesIO(response.content)).convert("RGB")
        image.thumbnail((512,512))
        return str(imagehash.phash(image))
    except Exception:
        return None
