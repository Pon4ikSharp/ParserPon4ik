CREATE TABLE IF NOT EXISTS telegram_lexicon_terms_v1 (
    channel_key text NOT NULL,
    source_id bigint,
    channel_title text NOT NULL DEFAULT '',
    term_type text NOT NULL,
    term text NOT NULL,
    count_24h integer NOT NULL DEFAULT 0,
    count_7d integer NOT NULL DEFAULT 0,
    target_docs_7d integer NOT NULL DEFAULT 0,
    global_channels_7d integer NOT NULL DEFAULT 1,
    burst_score double precision NOT NULL DEFAULT 0,
    candidate_score integer NOT NULL DEFAULT 0,
    first_seen_at timestamptz,
    last_seen_at timestamptz,
    example_document_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    promoted boolean NOT NULL DEFAULT false,
    snapshot_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(channel_key,term_type,term)
);
CREATE INDEX IF NOT EXISTS idx_tg_lexicon_score
    ON telegram_lexicon_terms_v1(candidate_score DESC,count_7d DESC);
CREATE INDEX IF NOT EXISTS idx_tg_lexicon_channel
    ON telegram_lexicon_terms_v1(channel_key,candidate_score DESC);
CREATE INDEX IF NOT EXISTS idx_tg_lexicon_term
    ON telegram_lexicon_terms_v1(term_type,term);

