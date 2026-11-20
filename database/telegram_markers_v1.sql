CREATE TABLE IF NOT EXISTS telegram_document_markers_v1 (
    source_document_id bigint NOT NULL,
    source_id bigint,
    channel_title text NOT NULL DEFAULT '',
    marker_type text NOT NULL,
    marker text NOT NULL,
    normalized_marker text NOT NULL,
    marker_index integer NOT NULL DEFAULT 0,
    extracted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(source_document_id,marker_type,normalized_marker)
);
CREATE INDEX IF NOT EXISTS idx_tg_markers_source
    ON telegram_document_markers_v1(source_id,marker_type,normalized_marker);
CREATE INDEX IF NOT EXISTS idx_tg_markers_marker
    ON telegram_document_markers_v1(marker_type,normalized_marker);
CREATE INDEX IF NOT EXISTS idx_tg_markers_doc
    ON telegram_document_markers_v1(source_document_id);
