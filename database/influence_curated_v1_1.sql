-- Influence curated semantic layer v1.1 (2026-09-23)
-- Idempotent persistence for curated canonical-document overrides.

BEGIN;

ALTER TABLE influence_story_curation_v1
    ADD COLUMN IF NOT EXISTS curated_canonical_document_id BIGINT REFERENCES source_documents(id);

UPDATE influence_story_curation_v1 AS c
SET curated_canonical_document_id = s.canonical_document_id
FROM grok_stories_v1 AS s
WHERE s.id = c.story_id
  AND c.curated_canonical_document_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_influence_story_curation_curated_canonical
    ON influence_story_curation_v1(curated_canonical_document_id);

COMMENT ON COLUMN influence_story_curation_v1.curated_canonical_document_id IS
    'Curated representative document for the Story. Overrides raw Grok canonical in the gold view without mutating raw Grok semantic tables.';

CREATE OR REPLACE VIEW influence_curated_story_view_v1 AS
SELECT
    c.story_id,
    c.included,
    c.curation_status,
    c.exclusion_reason,
    c.audience_context,
    c.propagation_note,
    c.chronology_note,
    s.story_key,
    s.title,
    s.summary,
    COALESCE(c.curated_canonical_document_id, s.canonical_document_id) AS canonical_document_id,
    s.first_seen_at,
    s.last_seen_at,
    st.relation,
    st.confidence,
    t.id AS thesis_id,
    t.thesis_key,
    t.label AS thesis_label,
    t.statement AS thesis_statement,
    n.id AS narrative_id,
    n.narrative_key,
    n.label AS narrative_label,
    d.id AS direction_id,
    d.direction_key,
    d.label AS direction_label
FROM influence_story_curation_v1 AS c
JOIN grok_stories_v1 AS s ON s.id = c.story_id
LEFT JOIN influence_story_theses_curated_v1 AS st ON st.story_id = c.story_id
LEFT JOIN influence_theses_curated_v1 AS t ON t.id = st.thesis_id
LEFT JOIN influence_narratives_curated_v1 AS n ON n.id = t.narrative_id
LEFT JOIN influence_directions_v1 AS d ON d.id = n.direction_id;

COMMIT;
