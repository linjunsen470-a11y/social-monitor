CREATE SCHEMA IF NOT EXISTS catering_watch;
CREATE TABLE IF NOT EXISTS catering_watch.projects (
  project_key text PRIMARY KEY, display_name text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS catering_watch.accounts (
  id bigserial PRIMARY KEY, project_key text NOT NULL REFERENCES catering_watch.projects(project_key),
  platform text NOT NULL CHECK (platform IN ('douyin','channels','xiaohongshu')),
  platform_account_id text NOT NULL, current_name text, profile_url text, verification_text text, bio text,
  ownership_status text NOT NULL DEFAULT 'unverified', first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(), last_checked_at timestamptz,
  last_successful_fetch_at timestamptz, source_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE(project_key, platform, platform_account_id)
);
CREATE TABLE IF NOT EXISTS catering_watch.account_aliases (
  id bigserial PRIMARY KEY, project_key text NOT NULL REFERENCES catering_watch.projects(project_key),
  account_id bigint NOT NULL REFERENCES catering_watch.accounts(id) ON DELETE CASCADE,
  alias text NOT NULL, observed_at timestamptz NOT NULL DEFAULT now(), source_url text,
  UNIQUE(project_key, account_id, alias)
);
CREATE TABLE IF NOT EXISTS catering_watch.posts (
  id bigserial PRIMARY KEY, project_key text NOT NULL REFERENCES catering_watch.projects(project_key),
  account_id bigint REFERENCES catering_watch.accounts(id) ON DELETE SET NULL,
  platform text NOT NULL CHECK (platform IN ('douyin','channels','xiaohongshu')),
  platform_post_id text NOT NULL, post_url text, published_at timestamptz, title text, body_text text,
  cover_url text, duration_seconds numeric, first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(), last_checked_at timestamptz, source_updated_at timestamptz,
  content_hash text, metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE(project_key, platform, platform_post_id)
);
CREATE TABLE IF NOT EXISTS catering_watch.post_features (
  id bigserial PRIMARY KEY, project_key text NOT NULL REFERENCES catering_watch.projects(project_key),
  post_id bigint NOT NULL REFERENCES catering_watch.posts(id) ON DELETE CASCADE,
  feature_version text NOT NULL, normalized_title_hash text, normalized_body_hash text,
  cover_phash text, ocr_text text, extracted_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(project_key, post_id, feature_version)
);
CREATE TABLE IF NOT EXISTS catering_watch.fetch_runs (
  id bigserial PRIMARY KEY, project_key text NOT NULL REFERENCES catering_watch.projects(project_key),
  platform text NOT NULL, operation text NOT NULL, input_fingerprint text NOT NULL, status text NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz, result_count integer NOT NULL DEFAULT 0,
  error_code text, error_message text, reserved_usd numeric NOT NULL, provider_run_id text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb, UNIQUE(project_key, provider_run_id)
);
CREATE TABLE IF NOT EXISTS catering_watch.candidates (
  id bigserial PRIMARY KEY, project_key text NOT NULL REFERENCES catering_watch.projects(project_key),
  source_post_id bigint NOT NULL REFERENCES catering_watch.posts(id) ON DELETE CASCADE,
  target_post_id bigint NOT NULL REFERENCES catering_watch.posts(id) ON DELETE CASCADE,
  discovery_method text NOT NULL, status text NOT NULL DEFAULT 'pending', score numeric,
  reasons jsonb NOT NULL DEFAULT '[]'::jsonb, first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(project_key, source_post_id, target_post_id)
);
CREATE TABLE IF NOT EXISTS catering_watch.matches (
  id bigserial PRIMARY KEY, project_key text NOT NULL REFERENCES catering_watch.projects(project_key),
  candidate_id bigint NOT NULL UNIQUE REFERENCES catering_watch.candidates(id) ON DELETE CASCADE,
  video_relation text NOT NULL DEFAULT 'unreviewed', ownership_relation text NOT NULL DEFAULT 'unverified',
  confidence numeric, decision_method text NOT NULL DEFAULT 'rules', rationale text,
  reviewed_at timestamptz, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS catering_watch.match_evidence (
  id bigserial PRIMARY KEY, project_key text NOT NULL REFERENCES catering_watch.projects(project_key),
  match_id bigint NOT NULL REFERENCES catering_watch.matches(id) ON DELETE CASCADE,
  evidence_type text NOT NULL, evidence_value jsonb NOT NULL, source_url text,
  observed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS catering_watch.cost_ledger (
  id bigserial PRIMARY KEY, project_key text NOT NULL REFERENCES catering_watch.projects(project_key),
  fetch_run_id bigint REFERENCES catering_watch.fetch_runs(id) ON DELETE SET NULL,
  provider text NOT NULL, actor_name text, operation text, units numeric NOT NULL DEFAULT 0,
  unit_price_usd numeric NOT NULL DEFAULT 0, amount_usd numeric NOT NULL DEFAULT 0,
  estimated boolean NOT NULL DEFAULT true, recorded_at timestamptz NOT NULL DEFAULT now(),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS catering_watch.review_queue (
  id bigserial PRIMARY KEY, project_key text NOT NULL REFERENCES catering_watch.projects(project_key),
  candidate_id bigint NOT NULL REFERENCES catering_watch.candidates(id) ON DELETE CASCADE,
  reason text NOT NULL, status text NOT NULL DEFAULT 'open', assigned_at timestamptz,
  resolved_at timestamptz, resolution text, UNIQUE(project_key, candidate_id, reason)
);
CREATE TABLE IF NOT EXISTS catering_watch.dataset_pages (
  fetch_id bigint REFERENCES catering_watch.fetch_runs(id) ON DELETE CASCADE, page_offset integer,
  items jsonb NOT NULL, PRIMARY KEY(fetch_id,page_offset)
);
CREATE TABLE IF NOT EXISTS catering_watch.tracked_accounts (
  project_key text REFERENCES catering_watch.projects, brand text, platform text, stable_id text,
  evidence_url text, synced_at timestamptz, next_cursor text, backfill_done boolean NOT NULL DEFAULT false,
  PRIMARY KEY(project_key,brand,platform,stable_id)
);
CREATE TABLE IF NOT EXISTS catering_watch.research_reports (
  id uuid PRIMARY KEY, project_key text NOT NULL REFERENCES catering_watch.projects,
  created_at timestamptz NOT NULL DEFAULT now(), evidence jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS catering_watch.post_aliases (
 project_key text REFERENCES catering_watch.projects, platform text, kind text, value text,
 canonical_id text NOT NULL, PRIMARY KEY(project_key,platform,kind,value)
);

CREATE INDEX IF NOT EXISTS idx_accounts_project_platform ON catering_watch.accounts(project_key, platform);
CREATE INDEX IF NOT EXISTS idx_posts_project_platform_published ON catering_watch.posts(project_key, platform, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_account_published ON catering_watch.posts(account_id, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON catering_watch.candidates(project_key, status, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_fetch_runs_fingerprint ON catering_watch.fetch_runs(project_key, input_fingerprint);
INSERT INTO catering_watch.projects(project_key, display_name)
VALUES ('guangzhou_catering_competitors','广州及周边团餐配送竞品监测')
ON CONFLICT (project_key) DO NOTHING;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='catering_watch_app') THEN
    GRANT USAGE ON SCHEMA catering_watch TO catering_watch_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA catering_watch TO catering_watch_app;
    GRANT USAGE ON ALL SEQUENCES IN SCHEMA catering_watch TO catering_watch_app;
  END IF;
END $$;
