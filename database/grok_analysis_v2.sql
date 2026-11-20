-- Grok Analysis v2: resumable run tracking + low-confidence story review.

CREATE TABLE IF NOT EXISTS grok_analysis_runs_v1 (
  run_key TEXT PRIMARY KEY,
  worker TEXT NOT NULL DEFAULT 'grok-routine',
  status TEXT NOT NULL DEFAULT 'open'
    CHECK(status IN ('open','completed','failed','cancelled')),
  requested_count INTEGER NOT NULL CHECK(requested_count BETWEEN 1 AND 50),
  claimed_count INTEGER NOT NULL DEFAULT 0,
  processed_count INTEGER NOT NULL DEFAULT 0,
  meta JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_grok_runs_worker_status
  ON grok_analysis_runs_v1(worker,status,created_at DESC);

CREATE TABLE IF NOT EXISTS grok_analysis_run_items_v1 (
  run_key TEXT NOT NULL REFERENCES grok_analysis_runs_v1(run_key) ON DELETE CASCADE,
  source_document_id BIGINT NOT NULL REFERENCES source_documents(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'claimed'
    CHECK(status IN ('claimed','analyzed','done')),
  error TEXT NOT NULL DEFAULT '',
  claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  analyzed_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  PRIMARY KEY(run_key,source_document_id),
  UNIQUE(run_key,ordinal)
);

CREATE INDEX IF NOT EXISTS idx_grok_run_items_doc
  ON grok_analysis_run_items_v1(source_document_id,status);

CREATE TABLE IF NOT EXISTS grok_story_review_v1 (
  source_document_id BIGINT PRIMARY KEY REFERENCES source_documents(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','resolved','dismissed')),
  reason TEXT NOT NULL DEFAULT '',
  confidence SMALLINT NOT NULL DEFAULT 0 CHECK(confidence BETWEEN 0 AND 100),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_grok_story_review_status
  ON grok_story_review_v1(status,updated_at DESC);
