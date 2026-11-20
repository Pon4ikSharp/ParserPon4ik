INSERT INTO calendar_events(
 canonical_key,title,summary,date_precision,status,confidence,first_seen_at,created_at,updated_at)
SELECT event_key,left(regexp_replace(event_text,'^#подія\s*','','i'),500),event_text,
       'unknown','tentative',40,created_at::timestamptz,created_at::timestamptz,created_at::timestamptz
FROM detected_events ON CONFLICT(canonical_key) DO NOTHING;

INSERT INTO calendar_event_sources(event_id,source_url,evidence,observed_at)
SELECT e.id,d.source_url,jsonb_build_object('legacy',true,'source_title',d.source_title),d.created_at::timestamptz
FROM detected_events d JOIN calendar_events e ON e.canonical_key=d.event_key
ON CONFLICT(event_id,source_url) DO NOTHING;

INSERT INTO calendar_event_revisions(event_id,revision_no,snapshot,changed_fields,reason,model,created_at)
SELECT e.id,1,to_jsonb(e),jsonb_build_array('legacy_import'),
       'Імпортовано з попереднього журналу подій','legacy',e.created_at
FROM calendar_events e
WHERE NOT EXISTS(SELECT 1 FROM calendar_event_revisions r WHERE r.event_id=e.id);

-- Some early snapshots contain a complete analysis with an empty status.
-- The analysis is already finished; restore the missing terminal state.
UPDATE info_influence
SET status=CASE
      WHEN length(trim(COALESCE(narrative_title,'')))>0
        OR role IN ('carrier','amplifier','reporting','trigger')
        THEN 'accepted'
      ELSE 'rejected'
    END,
    reason=CASE WHEN length(trim(COALESCE(reason,'')))>0 THEN reason
                ELSE 'Відновлено статус наявного результату первинного відбору' END,
    updated_at=COALESCE(updated_at,now()::text)
WHERE length(trim(COALESCE(status,'')))=0;
