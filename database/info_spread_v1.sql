ALTER TABLE primary_info_clusters_v1
    ADD COLUMN IF NOT EXISTS unique_domains integer NOT NULL DEFAULT 1;
ALTER TABLE primary_info_clusters_v1
    ADD COLUMN IF NOT EXISTS unique_telegram_channels integer NOT NULL DEFAULT 0;
ALTER TABLE primary_info_clusters_v1
    ADD COLUMN IF NOT EXISTS cross_platform boolean NOT NULL DEFAULT false;

ALTER TABLE primary_info_clusters_v1
    ADD COLUMN IF NOT EXISTS analysis_document_id bigint;
CREATE INDEX IF NOT EXISTS idx_primary_info_clusters_analysis_document
    ON primary_info_clusters_v1(analysis_document_id);
ALTER TABLE primary_info_clusters_v1
    ADD COLUMN IF NOT EXISTS signal_document_id bigint;
CREATE INDEX IF NOT EXISTS idx_primary_info_clusters_signal_document
    ON primary_info_clusters_v1(signal_document_id);
