import re
import socket
import unicodedata
from io import BytesIO
from ipaddress import ip_address

import httpx
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


def _download_cover(url, headers):
    target = httpx.URL(url)
    for _ in range(5):
        if (
            target.scheme not in {"http", "https"}
            or not target.host
            or target.userinfo
            or target.port not in (None, 80, 443)
        ):
            return None, "blocked_url"
        host = target.raw_host.decode("ascii")
        addresses = socket.getaddrinfo(
            host, target.port or (443 if target.scheme == "https" else 80), type=socket.SOCK_STREAM
        )
        ips = [ip_address(address[4][0]) for address in addresses]
        if not ips or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in ips):
            return None, "blocked_url"
        # Dial the checked address. Preserve the original host for HTTP and TLS verification.
        with (
            httpx.Client(timeout=12, trust_env=False) as client,
            client.stream(
                "GET",
                target.copy_with(host=str(ips[0])),
                headers={**headers, "Host": target.netloc.decode("ascii")},
                extensions={"sni_hostname": host},
                follow_redirects=False,
            ) as response,
        ):
            if response.has_redirect_location:
                target = target.join(response.headers["location"])
                continue
            if not response.is_success:
                return None, f"http_{response.status_code}"
            data = BytesIO()
            for chunk in response.iter_bytes(chunk_size=65536):
                if data.tell() + len(chunk) > 8_000_000:
                    return None, "too_large"
                data.write(chunk)
            data.seek(0)
            return data, "ok"
    return None, "too_many_redirects"


def cover_feature(url, platform=None):
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
        data, status = _download_cover(url, headers)
        if data is None:
            return None, status
        with Image.open(data) as image:
            image.thumbnail((512, 512))
            return str(imagehash.phash(image.convert("RGB"))), "ok"
    except (httpx.RequestError, socket.gaierror):
        return None, "network_error"
    except httpx.InvalidURL:
        return None, "blocked_url"
    except (OSError, ValueError, Image.DecompressionBombError):
        return None, "decode_error"
