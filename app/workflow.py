"""PostgreSQL queues shared by collector, editor and analyst."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def source_document_id(connection, source_kind, chat_id, message_id):
    row = connection.execute(
        """SELECT id FROM source_documents
           WHERE source_kind=? AND source_chat_id=? AND source_message_id=?""",
        (source_kind, chat_id, message_id),
    ).fetchone()
    return row["id"] if row else None


def enqueue_note_draft(
    connection,
    *,
    source_kind,
    source_chat_id,
    source_message_id,
    source_title,
    source_url,
    analysis,
    suggested_action="new",
    target_news_id=None,
):
    document_id = source_document_id(
        connection, source_kind, source_chat_id, source_message_id
    )
    serialized = _json(analysis)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    row = connection.execute(
        """INSERT INTO note_drafts(
             source_document_id,source_kind,source_chat_id,source_message_id,
             source_title,source_url,content_hash,analysis,suggested_action,target_news_id)
           VALUES (?,?,?,?,?,?,?,?::jsonb,?,?)
           ON CONFLICT(source_kind,source_chat_id,source_message_id,content_hash)
           DO UPDATE SET analysis=excluded.analysis,suggested_action=excluded.suggested_action,
             target_news_id=excluded.target_news_id,
             status=CASE WHEN note_drafts.status='failed' THEN 'queued' ELSE note_drafts.status END,
             error=CASE WHEN note_drafts.status='failed' THEN '' ELSE note_drafts.error END
           RETURNING id,status""",
        (
            document_id,
            source_kind,
            source_chat_id,
            source_message_id,
            source_title,
            source_url,
            digest,
            serialized,
            suggested_action,
            target_news_id,
        ),
    ).fetchone()
    connection.commit()
    return row


def enqueue_event_candidate(
    connection,
    *,
    source_kind,
    source_chat_id,
    source_message_id,
    candidate_key,
    candidate_text,
    source_url,
    payload,
    note_draft_id=None,
):
    document_id = source_document_id(
        connection, source_kind, source_chat_id, source_message_id
    )
    row = connection.execute(
        """INSERT INTO event_candidates(
             source_document_id,note_draft_id,candidate_key,candidate_text,payload)
           VALUES (?,?,?,?,?::jsonb)
           ON CONFLICT(source_document_id,candidate_key) DO UPDATE SET
             candidate_text=excluded.candidate_text,payload=excluded.payload,
             note_draft_id=COALESCE(excluded.note_draft_id,event_candidates.note_draft_id),
             status=CASE WHEN event_candidates.status='failed' THEN 'queued' ELSE event_candidates.status END,
             error=CASE WHEN event_candidates.status='failed' THEN '' ELSE event_candidates.error END
           RETURNING id,status""",
        (
            document_id,
            note_draft_id,
            candidate_key.strip().lower(),
            candidate_text,
            _json({**payload, "source_url": source_url}),
        ),
    ).fetchone()
    connection.commit()
    return row


def claim(connection, table, worker_status="processing"):
    if table not in {"note_drafts", "event_candidates"}:
        raise ValueError(table)
    row = connection.execute(
        f"""SELECT * FROM {table} WHERE status='queued'
            ORDER BY attempts,created_at,id FOR UPDATE SKIP LOCKED LIMIT 1"""
    ).fetchone()
    if not row:
        connection.commit()
        return None
    connection.execute(
        f"UPDATE {table} SET status=?,attempts=attempts+1,locked_at=now(),error='' WHERE id=?",
        (worker_status, row["id"]),
    )
    connection.commit()
    return row


def finish(connection, table, row_id, status, error=""):
    if table not in {"note_drafts", "event_candidates"}:
        raise ValueError(table)
    connection.execute(
        f"UPDATE {table} SET status=?,error=?,completed_at=now(),locked_at=NULL WHERE id=?",
        (status, error[:4000], row_id),
    )
    connection.commit()


def recover_stale(connection, minutes=30):
    for table in ("note_drafts", "event_candidates"):
        connection.execute(
            f"""UPDATE {table} SET status='queued',locked_at=NULL,
                 error='Відновлено після перерваної роботи профілю'
               WHERE status='processing' AND locked_at<now()-(?||' minutes')::interval""",
            (str(minutes),),
        )
    connection.commit()


def submit_analysis_job(
    connection, job_type, prompt="", window_start=None, window_end=None,
    options=None, priority=100
):
    row = connection.execute(
        """INSERT INTO analysis_jobs(job_type,prompt,window_start,window_end,options,priority)
           VALUES (?,?,?,?,?::jsonb,?) RETURNING id""",
        (job_type, prompt, window_start, window_end, _json(options or {}), priority),
    ).fetchone()
    connection.commit()
    return row["id"]
