CREATE TABLE IF NOT EXISTS hold_v2_results_v1 (
    source_document_id BIGINT PRIMARY KEY REFERENCES source_documents(id) ON DELETE CASCADE,
    disposition TEXT NOT NULL CHECK (disposition IN ('PROMOTE_TO_AI','PROPAGATION_ONLY','REVIEW','ARCHIVE')),
    reason TEXT NOT NULL DEFAULT '',
    matched_story_id BIGINT NULL REFERENCES grok_stories_v1(id) ON DELETE SET NULL,
    matched_story_key TEXT NOT NULL DEFAULT '',
    canonical_document_id BIGINT NULL,
    token_dice DOUBLE PRECISION NULL,
    char_ratio DOUBLE PRECISION NULL,
    length_ratio DOUBLE PRECISION NULL,
    input_fingerprint TEXT NOT NULL,
    classifier_version TEXT NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_hold_v2_results_disposition
    ON hold_v2_results_v1(disposition, source_document_id);

CREATE INDEX IF NOT EXISTS idx_hold_v2_results_version
    ON hold_v2_results_v1(classifier_version);
