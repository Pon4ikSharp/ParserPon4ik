"""Durable, independent BR/INFO queue for primary-only processing."""
from __future__ import annotations

from datetime import datetime, timezone


CUTOFF_UTC = "2026-09-04T21:00:00+00:00"  # 05.09.2026 00:00 Kyiv
TERMINAL = {"not_required", "accepted", "rejected", "discarded"}


def enqueue(connection, document_id: int) -> bool:
    """Queue one archived original without changing source flags or old results."""
    row = connection.execute(
        """SELECT d.id,s.for_br,s.for_info,
                  m.status AS br_existing,i.status AS info_existing
             FROM source_documents d
             JOIN sources s ON s.id=d.source_id
             LEFT JOIN messages m
               ON m.chat_id=d.source_chat_id AND m.message_id=d.source_message_id
             LEFT JOIN info_influence i ON i.source_document_id=d.id
            WHERE d.id=? AND s.enabled AND (s.for_br OR s.for_info)
              AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                           NULLIF(d.created_at,'')::timestamptz)>=?::timestamptz
              AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                           NULLIF(d.created_at,'')::timestamptz)<=now()+interval '5 minutes'""",
        (document_id, CUTOFF_UTC),
    ).fetchone()
    if not row:
        return False

    def route_status(required, existing):
        if not required:
            return "not_required"
        if existing and existing != "error":
            return "accepted" if existing in {"accepted", "queued", "event_queued"} else "rejected"
        return "pending"

    br_status = route_status(bool(row["for_br"]), row["br_existing"])
    info_status = route_status(bool(row["for_info"]), row["info_existing"])
    complete = br_status in TERMINAL and info_status in TERMINAL
    connection.execute(
        """INSERT INTO classification_queue(
               source_document_id,for_br,for_info,status,screen_status,
               br_status,info_status,completed_at)
             VALUES (?,?,?,?,?,?,?,CASE WHEN ? THEN now() ELSE NULL END)
             ON CONFLICT(source_document_id) DO NOTHING""",
        (document_id, bool(row["for_br"]), bool(row["for_info"]),
         "completed" if complete else "pending",
         "completed" if complete else "pending", br_status, info_status, complete),
    )
    return True


def recompute(connection, document_id: int, reason: str = "") -> str:
    row = connection.execute(
        "SELECT br_status,info_status FROM classification_queue WHERE source_document_id=?",
        (document_id,),
    ).fetchone()
    if not row:
        return "missing"
    states = (row["br_status"], row["info_status"])
    if any(value == "paused_limit" for value in states):
        status = "paused_limit"
    elif any(value in {"processing", "screening"} for value in states):
        status = "processing"
    elif any(value in {"pending", "ready"} for value in states):
        status = "pending"
    elif any(value == "error" for value in states):
        status = "error"
    else:
        status = "completed"
    connection.execute(
        """UPDATE classification_queue SET status=?,reason=?,updated_at=now(),
                  locked_at=NULL,worker_profile='',
                  completed_at=CASE WHEN ? IN ('completed','error') THEN now() ELSE NULL END
             WHERE source_document_id=?""",
        (status, reason[:1000], status, document_id),
    )
    return status


def mark_rss_cycle(connection, *, started: bool, historical_remaining: bool = True):
    key = "primary_rss_backfill_state"
    value = (
        "scanning"
        if started
        else ("more_history" if historical_remaining else "sealed")
    )
    connection.execute(
        """INSERT INTO system_state(key,value,updated_at) VALUES (?,?,now())
             ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()""",
        (key, value),
    )
    connection.commit()


def rss_backfill_sealed(connection) -> bool:
    row = connection.execute(
        "SELECT value FROM system_state WHERE key='primary_rss_backfill_state'"
    ).fetchone()
    return bool(row and row["value"] == "sealed")


def backlog_count(connection) -> int:
    row = connection.execute(
        """SELECT count(*) AS n FROM classification_queue q
             JOIN source_documents d ON d.id=q.source_document_id
            WHERE q.status IN ('pending','processing','paused_limit')
              AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                           NULLIF(d.created_at,'')::timestamptz)
                  < now()-interval '30 minutes'"""
    ).fetchone()
    return int(row["n"] or 0) if row else 0

