CREATE TABLE IF NOT EXISTS info_prefilter_results_v1 (
    source_document_id bigint PRIMARY KEY REFERENCES source_documents(id) ON DELETE CASCADE,
    cluster_id bigint,
    canonical_document_id bigint NOT NULL,
    route text NOT NULL,
    hard_filter_reason text NOT NULL DEFAULT '',
    domain_score integer NOT NULL DEFAULT 0,
    influence_score integer NOT NULL DEFAULT 0,
    spread_score integer NOT NULL DEFAULT 0,
    actor_hits jsonb NOT NULL DEFAULT '[]'::jsonb,
    narrative_hits jsonb NOT NULL DEFAULT '[]'::jsonb,
    hashtag_hits jsonb NOT NULL DEFAULT '[]'::jsonb,
    reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
    rules_version text NOT NULL DEFAULT '',
    processed_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_info_prefilter_route
    ON info_prefilter_results_v1(route, processed_at DESC);

CREATE INDEX IF NOT EXISTS idx_info_prefilter_cluster
    ON info_prefilter_results_v1(cluster_id);

