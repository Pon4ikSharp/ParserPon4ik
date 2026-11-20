"""Durable queue primitives for Processing Engine v2.

This module is intentionally isolated from collector.py and the legacy
classification_queue. It reads source_documents and writes only
processing_v2_* tables.

The live collector does not import this module yet.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

PIPELINE_VERSION = "processing-v2.0.0-dev1"
DEFAULT_MAX_ATTEMPTS = 3

ACTIVE_STATUSES = {"pending", "processing", "retry"}
TERMINAL_STATUSES = {"completed", "skipped", "dead_letter"}
STAGES = {"prefilter", "hydrate", "analyze"}


def install_schema(connection) -> None:
    """Install the additive v2 schema on an explicitly supplied connection."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sql = (root / "database" / "processing_v2.sql").read_text(encoding="utf-8")
    connection.raw.execute(sql)
    connection.commit()


def initial_stage(document) -> str:
    """Every archived original enters the deterministic no-AI prefilter first."""
    return "prefilter"


def next_stage_after_prefilter(document) -> str:
    """Route a passed original after local filtering."""
    source_kind = (document.get("source_kind") or "").lower()
    full_text = (document.get("full_text") or "").strip()
    content_status = (document.get("content_status") or "").lower()

    if source_kind == "rss" and not full_text and content_status != "paywall":
        return "hydrate"
    return "analyze"


def _event(connection, job_id: int, from_status: str, to_status: str, *,
           worker: str = "", attempt: int = 0, detail=None, error: str = "") -> None:
    connection.execute(
        """INSERT INTO processing_v2_job_events(
               job_id,from_status,to_status,worker,attempt,detail,error
           ) VALUES (?,?,?,?,?,?::jsonb,?)""",
        (
            job_id,
            from_status,
            to_status,
            worker,
            attempt,
            json.dumps(detail or {}, ensure_ascii=False),
            error[:4000],
        ),
    )


def enqueue_document(connection, document_id: int, *,
                     pipeline_version: str = PIPELINE_VERSION,
                     stage: str | None = None,
                     priority: int = 100,
                     max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                     commit: bool = True) -> int | None:
    """Idempotently queue one source document for its first required stage."""
    document = connection.execute(
        """SELECT id,source_kind,content_hash,content_status,full_text
             FROM source_documents WHERE id=?""",
        (document_id,),
    ).fetchone()
    if not document:
        return None

    chosen_stage = stage or initial_stage(document)
    if chosen_stage not in STAGES:
        raise ValueError(f"Unknown processing v2 stage: {chosen_stage}")

    source_hash = (document["content_hash"] or "").strip()
    if not source_hash:
        raise ValueError(f"source_documents.id={document_id} has empty content_hash")

    row = connection.execute(
        """INSERT INTO processing_v2_jobs(
               source_document_id,pipeline_version,source_hash,stage,
               priority,max_attempts
           ) VALUES (?,?,?,?,?,?)
           ON CONFLICT(source_document_id,pipeline_version,source_hash,stage)
           DO NOTHING
           RETURNING id,status,attempts""",
        (
            document_id,
            pipeline_version,
            source_hash,
            chosen_stage,
            int(priority),
            int(max_attempts),
        ),
    ).fetchone()

    if row:
        job_id = int(row["id"])
        _event(connection, job_id, "", "pending", detail={"reason": "enqueue"})
    else:
        existing = connection.execute(
            """SELECT id FROM processing_v2_jobs
                WHERE source_document_id=? AND pipeline_version=?
                  AND source_hash=? AND stage=?""",
            (document_id, pipeline_version, source_hash, chosen_stage),
        ).fetchone()
        job_id = int(existing["id"]) if existing else None

    if commit:
        connection.commit()
    return job_id


def seed_missing(connection, *,
                 pipeline_version: str = PIPELINE_VERSION,
                 limit: int = 5000,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 commit: bool = True) -> int:
    """Seed missing v2 jobs from the archive without modifying source_documents.

    Every original enters the deterministic no-AI prefilter first. The
    prefilter later decides whether a passed item needs hydration or can go
    directly to analysis. Re-running this function is safe because the natural
    v2 job key is unique.
    """
    rows = connection.execute(
        """WITH candidates AS (
               SELECT
                   d.id,
                   d.content_hash,
                   'prefilter' AS stage
               FROM source_documents d
               WHERE length(trim(coalesce(d.content_hash,'')))>0
                 AND NOT EXISTS (
                     SELECT 1
                       FROM processing_v2_jobs j
                      WHERE j.source_document_id=d.id
                        AND j.pipeline_version=?
                        AND j.source_hash=d.content_hash
                        AND j.stage='prefilter'
                 )
               ORDER BY d.id
               LIMIT ?
           )
           INSERT INTO processing_v2_jobs(
               source_document_id,pipeline_version,source_hash,stage,max_attempts
           )
           SELECT id,?,content_hash,stage,?
             FROM candidates
           ON CONFLICT DO NOTHING
           RETURNING id""",
        (pipeline_version, int(limit), pipeline_version, int(max_attempts)),
    ).fetchall()

    for row in rows:
        _event(connection, int(row["id"]), "", "pending", detail={"reason": "seed_missing"})

    if commit:
        connection.commit()
    return len(rows)


def seed_recent(connection, *,
                pipeline_version: str = PIPELINE_VERSION,
                limit: int = 500,
                priority: int = 10,
                max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                commit: bool = True) -> int:
    """Seed the newest unqueued originals for live/recent processing.

    This is intentionally separate from seed_missing(), which walks the archive
    oldest-first for backfill. Ordering by source_documents.id DESC prioritizes
    the newest ingested originals without relying on legacy TEXT timestamps.
    """
    rows = connection.execute(
        """WITH candidates AS (
               SELECT d.id,d.content_hash
                 FROM source_documents d
                WHERE length(trim(coalesce(d.content_hash,'')))>0
                  AND NOT EXISTS (
                      SELECT 1
                        FROM processing_v2_jobs j
                       WHERE j.source_document_id=d.id
                         AND j.pipeline_version=?
                         AND j.source_hash=d.content_hash
                         AND j.stage='prefilter'
                  )
                ORDER BY d.id DESC
                LIMIT ?
           )
           INSERT INTO processing_v2_jobs(
               source_document_id,pipeline_version,source_hash,stage,
               priority,max_attempts
           )
           SELECT id,?,content_hash,'prefilter',?,?
             FROM candidates
           ON CONFLICT DO NOTHING
           RETURNING id""",
        (
            pipeline_version,
            int(limit),
            pipeline_version,
            int(priority),
            int(max_attempts),
        ),
    ).fetchall()

    for row in rows:
        _event(
            connection,
            int(row["id"]),
            "",
            "pending",
            detail={"reason": "seed_recent"},
        )

    if commit:
        connection.commit()
    return len(rows)


def store_prefilter_result(connection, *, source_document_id: int,
                           pipeline_version: str,
                           source_hash: str,
                           input_basis: str,
                           result: dict,
                           canonical_document_id: int | None = None,
                           similarity: float | None = None,
                           details: dict | None = None,
                           commit: bool = True) -> int:
    """Persist the deterministic no-AI prefilter result."""
    decision = str(result.get("decision") or "")
    if decision not in {"pass", "drop", "duplicate_exact", "duplicate_near"}:
        raise ValueError(f"Unknown prefilter decision: {decision}")
    if input_basis not in {"source_text", "feed_text", "full_text", "manual"}:
        raise ValueError(f"Unknown input_basis: {input_basis}")

    row = connection.execute(
        """INSERT INTO processing_v2_prefilter_results(
               source_document_id,pipeline_version,source_hash,
               decision,reason,input_basis,
               char_count,word_count,url_count,exact_key,fingerprint64,
               canonical_document_id,similarity,details
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?, ?,?,?::jsonb)
           ON CONFLICT(source_document_id,pipeline_version,source_hash)
           DO UPDATE SET
               decision=excluded.decision,
               reason=excluded.reason,
               input_basis=excluded.input_basis,
               char_count=excluded.char_count,
               word_count=excluded.word_count,
               url_count=excluded.url_count,
               exact_key=excluded.exact_key,
               fingerprint64=excluded.fingerprint64,
               canonical_document_id=excluded.canonical_document_id,
               similarity=excluded.similarity,
               details=excluded.details,
               updated_at=now()
           RETURNING id""",
        (
            source_document_id,
            pipeline_version,
            source_hash,
            decision,
            str(result.get("reason") or ""),
            input_basis,
            max(0, int(result.get("char_count") or 0)),
            max(0, int(result.get("word_count") or 0)),
            max(0, int(result.get("url_count") or 0)),
            str(result.get("exact_key") or ""),
            str(result.get("fingerprint64") or ""),
            canonical_document_id,
            similarity,
            json.dumps(details or {}, ensure_ascii=False),
        ),
    ).fetchone()
    if commit:
        connection.commit()
    return int(row["id"])


def route_passed_prefilter(connection, document_id: int, *,
                           pipeline_version: str = PIPELINE_VERSION,
                           priority: int = 100,
                           commit: bool = True) -> int | None:
    """Queue the next non-AI/AI stage for a document that passed prefilter."""
    document = connection.execute(
        """SELECT id,source_kind,content_hash,content_status,full_text
             FROM source_documents WHERE id=?""",
        (document_id,),
    ).fetchone()
    if not document:
        return None
    stage = next_stage_after_prefilter(document)
    return enqueue_document(
        connection,
        document_id,
        pipeline_version=pipeline_version,
        stage=stage,
        priority=priority,
        commit=commit,
    )


def claim_batch(connection, worker: str, *,
                pipeline_version: str = PIPELINE_VERSION,
                stage: str = "analyze",
                limit: int = 20,
                commit: bool = True):
    """Atomically claim ready work with PostgreSQL SKIP LOCKED semantics."""
    if stage not in STAGES:
        raise ValueError(f"Unknown processing v2 stage: {stage}")
    if not worker.strip():
        raise ValueError("worker name is required")

    rows = connection.execute(
        """WITH picked AS (
               SELECT id,status AS old_status
                 FROM processing_v2_jobs
                WHERE pipeline_version=?
                  AND stage=?
                  AND status IN ('pending','retry')
                  AND next_attempt_at<=now()
                ORDER BY priority ASC, created_at ASC, id ASC
                FOR UPDATE SKIP LOCKED
                LIMIT ?
           )
           UPDATE processing_v2_jobs j
              SET status='processing',
                  attempts=j.attempts+1,
                  locked_at=now(),
                  locked_by=?,
                  updated_at=now(),
                  last_error=''
             FROM picked
            WHERE j.id=picked.id
           RETURNING j.*,picked.old_status""",
        (pipeline_version, stage, int(limit), worker),
    ).fetchall()

    for row in rows:
        _event(
            connection,
            int(row["id"]),
            row["old_status"],
            "processing",
            worker=worker,
            attempt=int(row["attempts"]),
        )

    if commit:
        connection.commit()
    return rows


def complete_job(connection, job_id: int, *, worker: str = "",
                 detail=None, commit: bool = True) -> bool:
    row = connection.execute(
        """UPDATE processing_v2_jobs
              SET status='completed',locked_at=NULL,locked_by='',
                  completed_at=now(),updated_at=now(),last_error=''
            WHERE id=? AND status='processing'
        RETURNING attempts""",
        (job_id,),
    ).fetchone()
    if not row:
        return False

    _event(
        connection,
        job_id,
        "processing",
        "completed",
        worker=worker,
        attempt=int(row["attempts"]),
        detail=detail,
    )
    if commit:
        connection.commit()
    return True


def fail_job(connection, job_id: int, error: str, *,
             worker: str = "", retry_delay_seconds: int = 60,
             detail=None, commit: bool = True) -> str:
    job = connection.execute(
        """SELECT id,status,attempts,max_attempts
             FROM processing_v2_jobs WHERE id=? FOR UPDATE""",
        (job_id,),
    ).fetchone()
    if not job:
        return "missing"

    old_status = job["status"]
    attempts = int(job["attempts"])
    max_attempts = int(job["max_attempts"])
    next_status = "dead_letter" if attempts >= max_attempts else "retry"

    connection.execute(
        """UPDATE processing_v2_jobs
              SET status=?,
                  next_attempt_at=CASE
                    WHEN ?='retry' THEN now()+make_interval(secs=>?)
                    ELSE next_attempt_at
                  END,
                  locked_at=NULL,locked_by='',last_error=?,updated_at=now(),
                  completed_at=CASE WHEN ?='dead_letter' THEN now() ELSE NULL END
            WHERE id=?""",
        (
            next_status,
            next_status,
            max(0, int(retry_delay_seconds)),
            error[:4000],
            next_status,
            job_id,
        ),
    )
    _event(
        connection,
        job_id,
        old_status,
        next_status,
        worker=worker,
        attempt=attempts,
        detail=detail,
        error=error,
    )
    if commit:
        connection.commit()
    return next_status


def release_stale(connection, *, older_than_minutes: int = 30,
                  commit: bool = True) -> int:
    """Return abandoned processing jobs to retry without losing attempt history."""
    rows = connection.execute(
        """UPDATE processing_v2_jobs
              SET status=CASE
                    WHEN attempts>=max_attempts THEN 'dead_letter'
                    ELSE 'retry'
                  END,
                  locked_at=NULL,
                  locked_by='',
                  next_attempt_at=now(),
                  last_error=CASE
                    WHEN length(last_error)>0 THEN last_error
                    ELSE 'stale processing lock released'
                  END,
                  updated_at=now(),
                  completed_at=CASE
                    WHEN attempts>=max_attempts THEN now()
                    ELSE NULL
                  END
            WHERE status='processing'
              AND locked_at < now()-make_interval(mins=>?)
        RETURNING id,attempts,status""",
        (max(1, int(older_than_minutes)),),
    ).fetchall()

    for row in rows:
        _event(
            connection,
            int(row["id"]),
            "processing",
            row["status"],
            attempt=int(row["attempts"]),
            detail={"reason": "stale_lock_release"},
        )
    if commit:
        connection.commit()
    return len(rows)


def store_hydration_result(connection, *, source_document_id: int,
                           pipeline_version: str,
                           source_hash: str,
                           outcome: dict,
                           commit: bool = True) -> int:
    """Persist a no-AI hydration result without rewriting source_documents."""
    status = str(outcome.get("status") or "error")
    if status not in {"extracted", "partial", "paywall", "blocked", "error"}:
        raise ValueError(f"Unknown hydration status: {status}")

    row = connection.execute(
        """INSERT INTO processing_v2_hydration_results(
               source_document_id,pipeline_version,source_hash,
               requested_url,final_url,status,full_text,raw_html,
               metadata,error,used_proxy,duration_ms,fetched_at
           ) VALUES (?,?,?,?,?,?,?, ?,?::jsonb,?,?,?,now())
           ON CONFLICT(source_document_id,pipeline_version,source_hash)
           DO UPDATE SET
               requested_url=excluded.requested_url,
               final_url=excluded.final_url,
               status=excluded.status,
               full_text=excluded.full_text,
               raw_html=excluded.raw_html,
               metadata=excluded.metadata,
               error=excluded.error,
               used_proxy=excluded.used_proxy,
               duration_ms=excluded.duration_ms,
               fetched_at=now(),
               updated_at=now()
           RETURNING id""",
        (
            source_document_id,
            pipeline_version,
            source_hash,
            str(outcome.get("requested_url") or ""),
            str(outcome.get("final_url") or ""),
            status,
            str(outcome.get("full_text") or ""),
            str(outcome.get("raw_html") or ""),
            json.dumps(outcome.get("metadata") or {}, ensure_ascii=False),
            str(outcome.get("error") or "")[:4000],
            bool(outcome.get("used_proxy")),
            max(0, int(outcome.get("duration_ms") or 0)),
        ),
    ).fetchone()
    if commit:
        connection.commit()
    return int(row["id"])


def latest_hydration_result(connection, document_id: int, *,
                            pipeline_version: str = PIPELINE_VERSION):
    return connection.execute(
        """SELECT *
             FROM processing_v2_hydration_results
            WHERE source_document_id=? AND pipeline_version=?
            ORDER BY updated_at DESC,id DESC
            LIMIT 1""",
        (document_id, pipeline_version),
    ).fetchone()


def enqueue_analysis_after_hydration(connection, document_id: int, *,
                                     pipeline_version: str = PIPELINE_VERSION,
                                     priority: int = 100,
                                     commit: bool = True) -> int | None:
    """Queue analysis under the same archived source hash after hydration."""
    return enqueue_document(
        connection,
        document_id,
        pipeline_version=pipeline_version,
        stage="analyze",
        priority=priority,
        commit=commit,
    )


def acquire_run_guard(connection, run_key: str, *,
                      purpose: str,
                      commit: bool = True) -> bool:
    """Atomically consume a one-time run key.

    Replaying the same command with the same run key becomes a no-op before any
    AI call or job claim occurs.
    """
    key = (run_key or "").strip()
    if not key:
        raise ValueError("run_key must not be empty")
    row = connection.execute(
        """INSERT INTO processing_v2_run_guards(run_key,purpose)
           VALUES (?,?)
           ON CONFLICT(run_key) DO NOTHING
           RETURNING run_key""",
        (key, str(purpose)[:200]),
    ).fetchone()
    if commit:
        connection.commit()
    return bool(row)


def record_ai_call(connection, *, pipeline_version: str,
                   stage: str, model: str,
                   reasoning_effort: str = "",
                   verbosity: str = "",
                   document_count: int = 0,
                   input_chars: int = 0,
                   output_chars: int = 0,
                   input_tokens: int | None = None,
                   cached_input_tokens: int | None = None,
                   output_tokens: int | None = None,
                   duration_ms: int = 0,
                   success: bool = False,
                   error: str = "",
                   commit: bool = True) -> int:
    """Record v2 AI usage metadata without storing prompts or credentials."""
    row = connection.execute(
        """INSERT INTO processing_v2_ai_calls(
               pipeline_version,stage,model,reasoning_effort,verbosity,
               document_count,input_chars,output_chars,
               input_tokens,cached_input_tokens,output_tokens,
               duration_ms,success,error
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           RETURNING id""",
        (
            pipeline_version,
            stage,
            model,
            reasoning_effort,
            verbosity,
            max(0, int(document_count)),
            max(0, int(input_chars)),
            max(0, int(output_chars)),
            input_tokens,
            cached_input_tokens,
            output_tokens,
            max(0, int(duration_ms)),
            bool(success),
            str(error)[:4000],
        ),
    ).fetchone()
    if commit:
        connection.commit()
    return int(row["id"])


def store_analysis(connection, *, source_document_id: int,
                   pipeline_version: str,
                   source_hash: str,
                   input_basis: str,
                   input_chars: int,
                   result: dict,
                   model: str = "",
                   prompt_version: str = "",
                   duration_ms: int = 0,
                   commit: bool = True) -> int:
    """Persist one versioned document-level result.

    The complete structured output is retained in result JSONB while the
    three high-value routing decisions are duplicated into indexed columns.
    """
    if input_basis not in {
        "source_text",
        "feed_text",
        "full_text",
        "manual",
        "hydration_v2",
        "hydration_v2_partial",
    }:
        raise ValueError(f"Unknown input_basis: {input_basis}")

    briefing = result.get("briefing") or {}
    influence = result.get("influence") or {}
    event = result.get("event") or {}

    row = connection.execute(
        """INSERT INTO processing_v2_document_analysis(
               source_document_id,pipeline_version,source_hash,
               input_basis,input_chars,language,summary,
               briefing_relevant,briefing_score,
               influence_relevant,influence_score,
               event_relevant,event_score,
               entities,topics,event_data,influence_data,briefing_data,
               result,quality,model,prompt_version,duration_ms
           ) VALUES (
               ?,?,?,?,?,?,?,
               ?,?,
               ?,?,
               ?,?,
               ?::jsonb,?::jsonb,?::jsonb,?::jsonb,?::jsonb,
               ?::jsonb,?::jsonb,?,?,?
           )
           ON CONFLICT(source_document_id,pipeline_version,source_hash)
           DO UPDATE SET
               input_basis=excluded.input_basis,
               input_chars=excluded.input_chars,
               language=excluded.language,
               summary=excluded.summary,
               briefing_relevant=excluded.briefing_relevant,
               briefing_score=excluded.briefing_score,
               influence_relevant=excluded.influence_relevant,
               influence_score=excluded.influence_score,
               event_relevant=excluded.event_relevant,
               event_score=excluded.event_score,
               entities=excluded.entities,
               topics=excluded.topics,
               event_data=excluded.event_data,
               influence_data=excluded.influence_data,
               briefing_data=excluded.briefing_data,
               result=excluded.result,
               quality=excluded.quality,
               model=excluded.model,
               prompt_version=excluded.prompt_version,
               duration_ms=excluded.duration_ms,
               updated_at=now()
           RETURNING id""",
        (
            source_document_id,
            pipeline_version,
            source_hash,
            input_basis,
            max(0, int(input_chars)),
            str(result.get("language") or ""),
            str(result.get("summary") or ""),
            briefing.get("relevant"),
            briefing.get("score"),
            influence.get("relevant"),
            influence.get("score"),
            event.get("relevant"),
            event.get("score"),
            json.dumps(result.get("entities") or [], ensure_ascii=False),
            json.dumps(result.get("topics") or [], ensure_ascii=False),
            json.dumps(event, ensure_ascii=False),
            json.dumps(influence, ensure_ascii=False),
            json.dumps(briefing, ensure_ascii=False),
            json.dumps(result, ensure_ascii=False),
            json.dumps(result.get("quality") or {}, ensure_ascii=False),
            model,
            prompt_version,
            max(0, int(duration_ms)),
        ),
    ).fetchone()

    if commit:
        connection.commit()
    return int(row["id"])


def queue_stats(connection, *,
                pipeline_version: str = PIPELINE_VERSION):
    return connection.execute(
        """SELECT stage,status,count(*) AS jobs,
                  min(created_at) AS oldest_created_at,
                  count(*) FILTER (
                      WHERE status='processing'
                        AND locked_at<now()-interval '30 minutes'
                  ) AS stale_processing
             FROM processing_v2_jobs
            WHERE pipeline_version=?
            GROUP BY stage,status
            ORDER BY stage,status""",
        (pipeline_version,),
    ).fetchall()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
