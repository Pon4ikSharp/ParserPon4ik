-- Dzen News discovery / propagation layer v1.
BEGIN;

CREATE TABLE IF NOT EXISTS dzen_stories_v1 (
  cluster_id TEXT PRIMARY KEY,
  story_url TEXT NOT NULL,
  persistent_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  representative_source TEXT NOT NULL DEFAULT '',
  representative_url TEXT NOT NULL DEFAULT '',
  representative_doc_id TEXT NOT NULL DEFAULT '',
  publication_at TIMESTAMPTZ,
  rubric_id TEXT NOT NULL DEFAULT '',
  importance INTEGER NOT NULL DEFAULT 0,
  source_count INTEGER NOT NULL DEFAULT 0,
  discovery_score INTEGER NOT NULL DEFAULT 0,
  discovery_hits JSONB NOT NULL DEFAULT '[]',
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_fetched_at TIMESTAMPTZ,
  fetch_status TEXT NOT NULL DEFAULT 'discovered',
  fetch_error TEXT NOT NULL DEFAULT '',
  raw_meta JSONB NOT NULL DEFAULT '{}'
);CREATE INDEX IF NOT EXISTS idx_dzen_stories_publication
  ON dzen_stories_v1(publication_at DESC);
CREATE INDEX IF NOT EXISTS idx_dzen_stories_score
  ON dzen_stories_v1(discovery_score DESC,last_seen_at DESC);

CREATE TABLE IF NOT EXISTS dzen_story_sources_v1 (
  cluster_id TEXT NOT NULL REFERENCES dzen_stories_v1(cluster_id) ON DELETE CASCADE,
  source_doc_id TEXT NOT NULL,
  agency_id TEXT NOT NULL DEFAULT '',
  source_name TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  article_url TEXT NOT NULL DEFAULT '',
  published_at TIMESTAMPTZ,
  is_origin BOOLEAN NOT NULL DEFAULT false,
  is_representative BOOLEAN NOT NULL DEFAULT false,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  raw_meta JSONB NOT NULL DEFAULT '{}',
  PRIMARY KEY(cluster_id,source_doc_id)
);
CREATE INDEX IF NOT EXISTS idx_dzen_story_sources_url
  ON dzen_story_sources_v1(article_url);
CREATE INDEX IF NOT EXISTS idx_dzen_story_sources_name
  ON dzen_story_sources_v1(source_name);
CREATE TABLE IF NOT EXISTS dzen_story_matches_v1 (
  cluster_id TEXT NOT NULL REFERENCES dzen_stories_v1(cluster_id) ON DELETE CASCADE,
  grok_story_id BIGINT,
  source_document_id BIGINT,
  match_type TEXT NOT NULL DEFAULT 'candidate',
  confidence NUMERIC(5,2) NOT NULL DEFAULT 0,
  matched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  notes TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(cluster_id,match_type,source_document_id)
);
CREATE INDEX IF NOT EXISTS idx_dzen_matches_story
  ON dzen_story_matches_v1(grok_story_id);

COMMENT ON TABLE dzen_stories_v1 IS
  'Dzen News story discovery and spread metadata; does not replace source_documents.';
COMMENT ON TABLE dzen_story_sources_v1 IS
  'Publications grouped by Dzen under one news story, used as propagation evidence.';
COMMENT ON TABLE dzen_story_matches_v1 IS
  'Matches between Dzen stories and local documents / semantic Story objects.';

COMMIT;

-- Incremental Dzen-to-local integration support.
BEGIN;
CREATE INDEX IF NOT EXISTS idx_dzen_story_sources_last_seen
  ON dzen_story_sources_v1(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_dzen_story_sources_published
  ON dzen_story_sources_v1(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_dzen_matches_source_document
  ON dzen_story_matches_v1(source_document_id);
CREATE OR REPLACE VIEW dzen_primary_spread_v1 AS
SELECT pcm.cluster_id AS primary_cluster_id,
       dm.cluster_id AS dzen_cluster_id,
       ds.title AS dzen_title,
       ds.source_count AS dzen_source_count,
       ds.discovery_score,
       ds.publication_at,
       count(DISTINCT dm.source_document_id) AS local_match_count,
       max(dm.confidence) AS match_confidence
  FROM dzen_story_matches_v1 dm
  JOIN primary_info_cluster_members_v1 pcm
    ON pcm.source_document_id=dm.source_document_id
  JOIN dzen_stories_v1 ds ON ds.cluster_id=dm.cluster_id
 WHERE dm.match_type='deterministic_dzen_v2'
 GROUP BY pcm.cluster_id,dm.cluster_id,ds.title,ds.source_count,
          ds.discovery_score,ds.publication_at;
COMMIT;

BEGIN;
CREATE TABLE IF NOT EXISTS dzen_source_candidates_v1 (
  source_name TEXT PRIMARY KEY,
  publications_24h INTEGER NOT NULL DEFAULT 0,
  stories_24h INTEGER NOT NULL DEFAULT 0,
  relevant_stories_24h INTEGER NOT NULL DEFAULT 0,
  publications_7d INTEGER NOT NULL DEFAULT 0,
  stories_7d INTEGER NOT NULL DEFAULT 0,
  relevant_stories_7d INTEGER NOT NULL DEFAULT 0,
  matched_source_id BIGINT,
  matched_source_title TEXT NOT NULL DEFAULT '',
  source_match_confidence NUMERIC(6,5) NOT NULL DEFAULT 0,
  first_seen_at TIMESTAMPTZ,
  last_seen_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dzen_source_candidates_priority
  ON dzen_source_candidates_v1(relevant_stories_24h DESC,stories_24h DESC);
COMMIT;

BEGIN;
ALTER TABLE dzen_source_candidates_v1
  ADD COLUMN IF NOT EXISTS sample_article_url TEXT NOT NULL DEFAULT '';
ALTER TABLE dzen_source_candidates_v1
  ADD COLUMN IF NOT EXISTS sample_domain TEXT NOT NULL DEFAULT '';
COMMIT;
