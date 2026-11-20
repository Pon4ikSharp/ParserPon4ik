-- Preserve every existing accepted/rejected label; queue only unfinished work
-- published from 05.09.2026 00:00 Kyiv onward.
-- classification_queue is derived state, so an interrupted experimental
-- version is safely rebuilt after all workers have been stopped.
DELETE FROM classification_queue;

INSERT INTO classification_queue(
 source_document_id,for_br,for_info,status,screen_status,
 br_status,info_status,reason,completed_at
)
SELECT d.id,s.for_br,s.for_info,
 CASE WHEN (NOT s.for_br OR (m.status IS NOT NULL AND m.status<>'error'))
            AND (NOT s.for_info OR i.status IN ('accepted','rejected'))
      THEN 'completed' ELSE 'pending' END,
 CASE WHEN (NOT s.for_br OR (m.status IS NOT NULL AND m.status<>'error'))
            AND (NOT s.for_info OR i.status IN ('accepted','rejected'))
      THEN 'completed' ELSE 'pending' END,
 CASE WHEN NOT s.for_br THEN 'not_required'
      WHEN m.status IN ('accepted','queued','event_queued') THEN 'accepted'
      WHEN m.status IS NOT NULL AND m.status<>'error' THEN 'rejected'
      ELSE 'pending' END,
 CASE WHEN NOT s.for_info THEN 'not_required'
      WHEN i.status='accepted' THEN 'accepted'
      WHEN i.status='rejected' THEN 'rejected'
      ELSE 'pending' END,
 'Початкове заповнення незалежного BR/INFO router',
 CASE WHEN (NOT s.for_br OR (m.status IS NOT NULL AND m.status<>'error'))
            AND (NOT s.for_info OR i.status IN ('accepted','rejected'))
      THEN now() ELSE NULL END
FROM source_documents d
JOIN sources s ON s.id=d.source_id
LEFT JOIN messages m
  ON m.chat_id=d.source_chat_id AND m.message_id=d.source_message_id
LEFT JOIN info_influence i ON i.source_document_id=d.id
WHERE s.enabled AND (s.for_br OR s.for_info)
  AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
               NULLIF(d.created_at,'')::timestamptz)
      >= '2026-09-04T21:00:00+00:00'::timestamptz
  AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
               NULLIF(d.created_at,'')::timestamptz)
      <= now()+interval '5 minutes'
ON CONFLICT(source_document_id) DO NOTHING;

-- User explicitly chose to discard old error rows, while originals remain.
DELETE FROM messages m USING source_documents d
 WHERE m.chat_id=d.source_chat_id AND m.message_id=d.source_message_id
   AND m.status='error'
   AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                NULLIF(d.created_at,'')::timestamptz)
       >= '2026-09-04T21:00:00+00:00'::timestamptz;
DELETE FROM info_influence i USING source_documents d
 WHERE i.source_document_id=d.id AND i.status='error'
   AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                NULLIF(d.created_at,'')::timestamptz)
       >= '2026-09-04T21:00:00+00:00'::timestamptz;

UPDATE source_documents SET
 br_attempts=0,br_next_retry_at=NULL,br_last_error='',
 info_attempts=0,info_next_retry_at=NULL,info_last_error=''
WHERE COALESCE(NULLIF(published_at,'')::timestamptz,
               NULLIF(created_at,'')::timestamptz)
      >= '2026-09-04T21:00:00+00:00'::timestamptz;

-- A restart safely reclaims interrupted work.
UPDATE classification_queue SET
 status='pending',screen_status=CASE WHEN screen_status='processing' THEN 'pending' ELSE screen_status END,
 br_status=CASE WHEN br_status IN ('processing','screening') THEN 'pending' ELSE br_status END,
 info_status=CASE WHEN info_status IN ('processing','screening') THEN 'pending' ELSE info_status END,
 locked_at=NULL,worker_profile='',updated_at=now()
WHERE status='processing' OR screen_status='processing'
   OR br_status IN ('processing','screening') OR info_status IN ('processing','screening');

INSERT INTO system_state(key,value,updated_at)
VALUES ('primary_rss_backfill_state','scanning',now())
ON CONFLICT(key) DO UPDATE SET value='scanning',updated_at=now();
