import json
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import settings

SCHEMA_SQL = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


def connect():
    if (
        not settings.database_url
        or "<PASSWORD>" in settings.database_url
        or "<NEON_HOST>" in settings.database_url
    ):
        raise RuntimeError("DATABASE_URL 未配置。请使用 Neon catering_watch_app 角色连接串。")
    return psycopg.connect(settings.database_url, row_factory=dict_row, connect_timeout=10)


def init_db():
    with connect() as conn:
        conn.execute(SCHEMA_SQL)


def db_status():
    with connect() as conn:
        return conn.execute(
            """SELECT current_database() AS database, current_user AS role,
          (SELECT count(*) FROM catering_watch.accounts WHERE project_key=%s) AS accounts,
          (SELECT count(*) FROM catering_watch.posts WHERE project_key=%s) AS posts,
          (SELECT count(*) FROM catering_watch.candidates WHERE project_key=%s) AS candidates,
          (SELECT count(*) FROM catering_watch.fetch_runs WHERE project_key=%s) AS fetch_runs""",
            (settings.project_key,) * 4,
        ).fetchone()


def upsert_account(
    platform, account_id, name=None, url=None, verification=None, bio=None, source_metadata=None
):
    with connect() as conn:
        if platform == "channels" and not (source_metadata or {}).get("channelId"):
            # Feed rows often contain only username. Reuse the account identified by search.
            known = conn.execute(
                """SELECT platform_account_id FROM catering_watch.accounts WHERE project_key=%s
                AND platform='channels' AND platform_account_id=source_metadata->>'channelId'
                AND %s IN (source_metadata->>'finderUserName', source_metadata->>'username') LIMIT 2""",
                (settings.project_key, str(account_id)),
            ).fetchall()
            if len(known) == 1:
                account_id = known[0]["platform_account_id"]
        row = conn.execute(
            """INSERT INTO catering_watch.accounts
          (project_key,platform,platform_account_id,current_name,profile_url,verification_text,bio,source_metadata)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
          ON CONFLICT(project_key,platform,platform_account_id) DO UPDATE SET
            current_name=COALESCE(EXCLUDED.current_name,catering_watch.accounts.current_name),
            profile_url=COALESCE(EXCLUDED.profile_url,catering_watch.accounts.profile_url),
            verification_text=COALESCE(EXCLUDED.verification_text,catering_watch.accounts.verification_text),
            bio=COALESCE(EXCLUDED.bio,catering_watch.accounts.bio),last_seen_at=now(),
            source_metadata=catering_watch.accounts.source_metadata || EXCLUDED.source_metadata
          RETURNING id""",
            (
                settings.project_key,
                platform,
                str(account_id),
                name,
                url,
                verification,
                bio,
                Jsonb(source_metadata or {}),
            ),
        ).fetchone()
        if name:
            conn.execute(
                """INSERT INTO catering_watch.account_aliases(project_key,account_id,alias,source_url)
              VALUES (%s,%s,%s,%s) ON CONFLICT(project_key,account_id,alias) DO NOTHING""",
                (settings.project_key, row["id"], name, url),
            )
        return int(row["id"])


def upsert_post(post: dict) -> int:
    account_id = post.get("account_id")
    if not account_id and post.get("author_id"):
        account_id = upsert_account(
            post["platform"],
            post["author_id"],
            post.get("author_name"),
            post.get("author_url"),
            post.get("verification"),
            post.get("bio"),
            post.get("author_identifiers"),
        )
    with connect() as conn:
        if post["platform"] == "channels":
            raw = post.get("metadata", {}).get("raw_payload", {})
            aliases = {
                k: str(raw[k]) for k in ("docID", "videoId", "hashDocID", "exportId") if raw.get(k)
            }
            if aliases:
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (settings.project_key + ":channels-alias",),
                )
                known = set()
                for kind, value in aliases.items():
                    row = conn.execute(
                        """SELECT canonical_id FROM catering_watch.post_aliases
                        WHERE project_key=%s AND platform='channels' AND kind=%s AND value=%s""",
                        (settings.project_key, kind, value),
                    ).fetchone()
                    if row:
                        known.add(row["canonical_id"])
                if len(known) > 1:
                    raise ValueError("视频号 ID 别名冲突，需核对")
                post = dict(post, post_id=next(iter(known), post["post_id"]))
                for kind, value in aliases.items():
                    conn.execute(
                        """INSERT INTO catering_watch.post_aliases VALUES(%s,'channels',%s,%s,%s)
                        ON CONFLICT DO NOTHING""",
                        (settings.project_key, kind, value, post["post_id"]),
                    )
        row = conn.execute(
            """INSERT INTO catering_watch.posts
          (project_key,account_id,platform,platform_post_id,post_url,published_at,title,body_text,
           cover_url,duration_seconds,content_hash,metadata,last_seen_at,last_checked_at)
          VALUES (%s,%s,%s,%s,%s,to_timestamp(%s),%s,%s,%s,%s,%s,%s::jsonb,now(),now())
          ON CONFLICT(project_key,platform,platform_post_id) DO UPDATE SET
            account_id=COALESCE(EXCLUDED.account_id,catering_watch.posts.account_id),
            post_url=COALESCE(EXCLUDED.post_url,catering_watch.posts.post_url),
            published_at=COALESCE(EXCLUDED.published_at,catering_watch.posts.published_at),
            title=COALESCE(NULLIF(EXCLUDED.title,''),catering_watch.posts.title),
            body_text=COALESCE(NULLIF(EXCLUDED.body_text,''),catering_watch.posts.body_text),
            cover_url=COALESCE(EXCLUDED.cover_url,catering_watch.posts.cover_url),
            duration_seconds=COALESCE(EXCLUDED.duration_seconds,catering_watch.posts.duration_seconds),
            content_hash=COALESCE(EXCLUDED.content_hash,catering_watch.posts.content_hash),
            metadata=catering_watch.posts.metadata || EXCLUDED.metadata,last_seen_at=now(),last_checked_at=now()
          RETURNING id""",
            (
                settings.project_key,
                account_id,
                post["platform"],
                post["post_id"],
                post.get("url"),
                post.get("published_at"),
                post.get("title"),
                post.get("body"),
                post.get("cover_url"),
                post.get("duration_seconds"),
                post.get("content_hash"),
                json.dumps(post.get("metadata", {}), ensure_ascii=False),
            ),
        ).fetchone()
        return int(row["id"])


def find_cached_fetch(fingerprint):
    with connect() as conn:
        return conn.execute(
            """SELECT id,status,result_count,metadata,finished_at
          FROM catering_watch.fetch_runs WHERE project_key=%s AND input_fingerprint=%s
          AND status='SUCCEEDED'
          AND metadata ? 'post_db_ids'
          AND finished_at > now() - (%s || ' hours')::interval
          ORDER BY finished_at DESC LIMIT 1""",
            (
                settings.project_key,
                fingerprint,
                str(settings.cache_ttl_hours),
            ),
        ).fetchone()


def save_candidate_match(source_post_id, target_post_id, result, source_url, target_url):
    with connect() as conn:
        if result.get("relation") == "not_matched_by_metadata":
            found = conn.execute(
                """SELECT id FROM catering_watch.candidates WHERE project_key=%s
                AND source_post_id=%s AND target_post_id=%s""",
                (settings.project_key, source_post_id, target_post_id),
            ).fetchone()
            if not found:
                return None
        candidate = conn.execute(
            """INSERT INTO catering_watch.candidates
          (project_key,source_post_id,target_post_id,discovery_method,status,score,reasons)
          VALUES (%s,%s,%s,'cover_title_body','pending',%s,%s::jsonb)
          ON CONFLICT(project_key,source_post_id,target_post_id) DO UPDATE SET
            last_seen_at=now(),score=EXCLUDED.score,reasons=EXCLUDED.reasons RETURNING id""",
            (
                settings.project_key,
                source_post_id,
                target_post_id,
                result.get("score"),
                json.dumps(result.get("reasons", []), ensure_ascii=False),
            ),
        ).fetchone()
        match = conn.execute(
            """INSERT INTO catering_watch.matches
          (project_key,candidate_id,video_relation,ownership_relation,confidence,decision_method,rationale)
          VALUES (%s,%s,%s,'unverified',%s,'deterministic_rules',%s)
          ON CONFLICT(candidate_id) DO UPDATE SET video_relation=EXCLUDED.video_relation,
            confidence=EXCLUDED.confidence,rationale=EXCLUDED.rationale,updated_at=now() RETURNING id""",
            (
                settings.project_key,
                candidate["id"],
                result.get("relation", "unreviewed"),
                result.get("score"),
                result.get("warning"),
            ),
        ).fetchone()
        for typ, url, value in [
            ("source_post", source_url, {"url": source_url}),
            ("target_post", target_url, {"url": target_url}),
            ("metadata_match", source_url, result),
        ]:
            conn.execute(
                """INSERT INTO catering_watch.match_evidence
              (project_key,match_id,evidence_type,evidence_value,source_url)
              SELECT %s,%s,%s,%s::jsonb,%s WHERE NOT EXISTS (
                SELECT 1 FROM catering_watch.match_evidence WHERE match_id=%s AND evidence_type=%s
                AND evidence_value=%s::jsonb AND source_url IS NOT DISTINCT FROM %s)""",
                (
                    settings.project_key,
                    match["id"],
                    typ,
                    json.dumps(value, ensure_ascii=False),
                    url,
                    match["id"],
                    typ,
                    json.dumps(value, ensure_ascii=False),
                    url,
                ),
            )
        return int(candidate["id"])


POST_SELECT = """
    SELECT p.*, a.current_name AS author_name, a.platform_account_id AS author_id,
           a.profile_url AS author_url, a.verification_text, f.cover_phash
    FROM catering_watch.posts p
    LEFT JOIN catering_watch.accounts a ON a.id=p.account_id AND a.project_key=p.project_key
    LEFT JOIN catering_watch.post_features f ON f.post_id=p.id AND f.project_key=p.project_key
        AND f.feature_version='cover-phash-v1'
    WHERE p.project_key=%s
"""


def get_posts_by_ids(ids):
    if not ids:
        return []
    with connect() as conn:
        return conn.execute(
            POST_SELECT + " AND p.id=ANY(%s) ORDER BY p.id", (settings.project_key, ids)
        ).fetchall()


def stored_post(platform, post_id):
    with connect() as conn:
        return conn.execute(
            POST_SELECT + " AND p.platform=%s AND p.platform_post_id=%s",
            (settings.project_key, platform, post_id),
        ).fetchone()


def save_cover_phash(post_id, value):
    if not value:
        return
    with connect() as conn:
        conn.execute(
            """INSERT INTO catering_watch.post_features
            (project_key, post_id, feature_version, cover_phash)
            SELECT project_key,id,'cover-phash-v1',%s FROM catering_watch.posts
            WHERE project_key=%s AND id=%s ON CONFLICT(project_key,post_id,feature_version)
            DO UPDATE SET cover_phash=EXCLUDED.cover_phash, extracted_at=now()""",
            (value, settings.project_key, post_id),
        )


def get_cover_phash(post_id):
    with connect() as conn:
        row = conn.execute(
            """SELECT cover_phash FROM catering_watch.post_features
            WHERE project_key=%s AND post_id=%s AND feature_version='cover-phash-v1'""",
            (settings.project_key, post_id),
        ).fetchone()
        return row["cover_phash"] if row else None
