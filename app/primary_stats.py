"""Live statistics for collection + adaptive four-worker primary-analysis mode (v6.3)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from app.db import ROOT, connect

DEFAULT_SINCE = "2026-09-04T21:00:00+00:00"  # 05.09.2026 00:00 Europe/Kyiv
SNAPSHOT_PATH = ROOT / "runtime" / "primary_analysis_stats.json"


def _since(value: str | None = None) -> str:
    return value or os.getenv("PRIMARY_BACKLOG_SINCE", "").strip() or DEFAULT_SINCE


def collect(since: str | None = None) -> dict[str, object]:
    cutoff = _since(since)
    live_minutes = max(5, int(os.getenv("PRIMARY_LIVE_WINDOW_MINUTES", "30")))
    configured = max(1, min(8, int(os.getenv("PRIMARY_WORKERS", "4"))))
    database = connect(readonly=True)
    try:
        row = database.execute(
            """
            WITH eligible AS (
                SELECT d.id,d.source_kind,d.source_chat_id,d.source_message_id,
                       d.created_at,d.published_at,d.info_next_retry_at
                  FROM source_documents d
                  JOIN sources s ON s.id=d.source_id
                 WHERE s.enabled AND (s.for_info OR s.for_br)
                   AND (NULLIF(d.published_at,'') IS NULL
                        OR NULLIF(d.published_at,'')::timestamptz<=now()+interval '5 minutes')
                   AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                NULLIF(d.created_at,'')::timestamptz) >= ?::timestamptz
            ), joined AS (
                SELECT e.*,i.status,i.reason,i.created_at AS influence_created_at,
                       i.updated_at AS influence_updated_at,l.duplicate_type
                  FROM eligible e
                  LEFT JOIN info_influence i
                    ON i.source_kind=e.source_kind
                   AND i.source_chat_id=e.source_chat_id
                   AND i.source_message_id=e.source_message_id
                  LEFT JOIN primary_dedup_links l ON l.duplicate_document_id=e.id
            )
            SELECT
                count(*) AS total,
                count(*) FILTER (WHERE status IS NOT NULL) AS processed,
                count(*) FILTER (WHERE status IS NULL) AS pending,
                count(*) FILTER (
                    WHERE status IS NULL
                      AND COALESCE(NULLIF(published_at,'')::timestamptz,
                                   NULLIF(created_at,'')::timestamptz)
                          >= now()-(?||' minutes')::interval
                ) AS live_pending,
                count(*) FILTER (WHERE status IS NULL AND info_next_retry_at>now()) AS retry_waiting,
                count(*) FILTER (
                    WHERE status='accepted' AND duplicate_type IS NULL
                      AND COALESCE(reason,'') NOT LIKE 'Швидкий фільтр:%%'
                      AND COALESCE(reason,'') NOT LIKE 'Автоматично вилучено з черги після помилки%%'
                      AND COALESCE(reason,'') NOT LIKE 'Дублікат exact:%%'
                      AND COALESCE(reason,'') NOT LIKE 'Дублікат near:%%'
                ) AS accepted_by_analysis,
                count(*) FILTER (
                    WHERE status='rejected' AND duplicate_type IS NULL
                      AND COALESCE(reason,'') NOT LIKE 'Швидкий фільтр:%%'
                      AND COALESCE(reason,'') NOT LIKE 'Автоматично вилучено з черги після помилки%%'
                      AND COALESCE(reason,'') NOT LIKE 'Дублікат exact:%%'
                      AND COALESCE(reason,'') NOT LIKE 'Дублікат near:%%'
                ) AS rejected_by_analysis,
                count(*) FILTER (WHERE COALESCE(reason,'') LIKE 'Швидкий фільтр:%%') AS fast_skipped,
                count(*) FILTER (WHERE COALESCE(reason,'') LIKE 'Автоматично вилучено з черги після помилки%%') AS errors_removed,
                count(*) FILTER (WHERE status='error') AS active_errors,
                count(*) FILTER (WHERE duplicate_type='exact') AS exact_duplicates,
                count(*) FILTER (WHERE duplicate_type='near') AS near_duplicates,
                count(*) FILTER (
                    WHERE COALESCE(NULLIF(influence_updated_at,'')::timestamptz,
                                   NULLIF(influence_created_at,'')::timestamptz) >= now()-interval '1 hour'
                ) AS processed_last_hour,
                count(*) FILTER (
                    WHERE status IN ('accepted','rejected') AND duplicate_type IS NULL
                      AND COALESCE(reason,'') NOT LIKE 'Швидкий фільтр:%%'
                      AND COALESCE(reason,'') NOT LIKE 'Автоматично вилучено з черги після помилки%%'
                      AND COALESCE(reason,'') NOT LIKE 'Дублікат exact:%%'
                      AND COALESCE(reason,'') NOT LIKE 'Дублікат near:%%'
                      AND COALESCE(NULLIF(influence_updated_at,'')::timestamptz,
                                   NULLIF(influence_created_at,'')::timestamptz) >= now()-interval '1 hour'
                ) AS analyzed_last_hour,
                count(*) FILTER (
                    WHERE COALESCE(reason,'') LIKE 'Швидкий фільтр:%%'
                      AND COALESCE(NULLIF(influence_updated_at,'')::timestamptz,
                                   NULLIF(influence_created_at,'')::timestamptz) >= now()-interval '1 hour'
                ) AS fast_last_hour,
                count(*) FILTER (
                    WHERE duplicate_type='exact'
                      AND COALESCE(NULLIF(influence_updated_at,'')::timestamptz,
                                   NULLIF(influence_created_at,'')::timestamptz) >= now()-interval '1 hour'
                ) AS exact_last_hour,
                count(*) FILTER (
                    WHERE duplicate_type='near'
                      AND COALESCE(NULLIF(influence_updated_at,'')::timestamptz,
                                   NULLIF(influence_created_at,'')::timestamptz) >= now()-interval '1 hour'
                ) AS near_last_hour,
                count(*) FILTER (WHERE NULLIF(created_at,'')::timestamptz >= now()-interval '1 hour') AS db_created_last_hour,
                count(*) FILTER (
                    WHERE NULLIF(created_at,'')::timestamptz >= now()-interval '1 hour'
                      AND NULLIF(published_at,'') IS NOT NULL
                      AND NULLIF(published_at,'')::timestamptz >= now()-interval '1 hour'
                ) AS live_ingested_last_hour,
                count(*) FILTER (
                    WHERE NULLIF(created_at,'')::timestamptz >= now()-interval '1 hour'
                      AND NULLIF(published_at,'') IS NOT NULL
                      AND NULLIF(published_at,'')::timestamptz < now()-interval '1 hour'
                ) AS backfill_ingested_last_hour,
                count(*) FILTER (
                    WHERE NULLIF(created_at,'')::timestamptz >= now()-interval '1 hour'
                      AND NULLIF(published_at,'') IS NULL
                ) AS undated_ingested_last_hour,
                count(*) FILTER (
                    WHERE NULLIF(published_at,'') IS NOT NULL
                      AND NULLIF(published_at,'')::timestamptz >= now()-interval '1 hour'
                ) AS actually_published_last_hour
              FROM joined
            """,
            (cutoff, str(live_minutes)),
        ).fetchone()

        events = database.execute(
            """SELECT
                   count(*) FILTER (WHERE event_type='rate_limit') AS rate_limits,
                   count(*) FILTER (WHERE event_type='timeout') AS timeouts,
                   count(*) FILTER (WHERE event_type='transient_error') AS transient_errors,
                   count(*) FILTER (WHERE event_type='error_dropped') AS error_dropped_hour,
                   count(*) FILTER (WHERE event_type='dedup_wait') AS dedup_wait_hour
                 FROM primary_worker_events
                WHERE created_at>=now()-interval '1 hour'"""
        ).fetchone()
        ingest = database.execute(
            """SELECT count(*) AS rss_ingest_duplicates_hour
                 FROM rss_entries
                WHERE status='duplicate_ingest'
                  AND NULLIF(created_at,'')::timestamptz>=now()-interval '1 hour'"""
        ).fetchone()
        active = database.execute(
            """SELECT
                   count(*) AS active,
                   count(*) FILTER (WHERE label LIKE 'LIVE%') AS live_active,
                   count(*) FILTER (WHERE label NOT LIKE 'LIVE%') AS backlog_active
                 FROM primary_worker_state
                WHERE status='running' AND last_seen_at>=now()-interval '2 minutes'"""
        ).fetchone()
        database.commit()
    finally:
        database.close()

    stats = {key: int(row[key] or 0) for key in row.keys()}
    stats.update({key: int(events[key] or 0) for key in events.keys()})
    stats["rss_ingest_duplicates_hour"] = int(ingest["rss_ingest_duplicates_hour"] or 0)
    stats["workers_configured"] = configured
    stats["workers_active"] = int(active["active"] or 0)
    stats["live_workers"] = int(active["live_active"] or 0)
    stats["backlog_workers"] = int(active["backlog_active"] or 0)
    stats["codex_analyzed"] = stats["accepted_by_analysis"] + stats["rejected_by_analysis"]
    stats["dedup_saved"] = stats["exact_duplicates"] + stats["near_duplicates"]
    stats["dedup_saved_last_hour"] = stats["exact_last_hour"] + stats["near_last_hour"]
    stats["backlog_pending"] = max(0, stats["pending"] - stats["live_pending"])
    # All DB-created records affect the queue right now; the live margin answers
    # the separate question whether current publication flow is sustainable.
    stats["queue_net_last_hour"] = stats["processed_last_hour"] - stats["db_created_last_hour"]
    stats["live_capacity_margin_last_hour"] = stats["processed_last_hour"] - stats["live_ingested_last_hour"]
    stats["completion_percent"] = round(
        stats["processed"] * 100.0 / stats["total"] if stats["total"] else 100.0, 1
    )
    reserve = stats["live_capacity_margin_last_hour"]
    stats["eta_hours"] = round(stats["pending"] / reserve, 1) if reserve > 0 else None
    stats["since"] = cutoff
    stats["generated_at"] = datetime.now(timezone.utc).isoformat()
    return stats


def save_snapshot(stats: dict[str, object]) -> Path:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SNAPSHOT_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(SNAPSHOT_PATH)
    return SNAPSHOT_PATH


def format_log_line(stats: dict[str, object]) -> str:
    eta = f"{stats['eta_hours']}h" if stats["eta_hours"] is not None else "—"
    return (
        "[PRIMARY-STATS] "
        f"workers={stats['workers_active']}/{stats['workers_configured']} | "
        f"готово={stats['processed']}/{stats['total']} ({stats['completion_percent']}%) | "
        f"pending={stats['pending']} (LIVE={stats['live_pending']}, BACKLOG={stats['backlog_pending']}) | "
        f"Codex={stats['codex_analyzed']} | prefilter={stats['fast_skipped']} | "
        f"exact={stats['exact_duplicates']} near={stats['near_duplicates']} saved={stats['dedup_saved']} | "
        f"1h: db+={stats['db_created_last_hour']} live+={stats['live_ingested_last_hour']} "
        f"backfill+={stats['backfill_ingested_last_hour']} done={stats['processed_last_hour']} "
        f"queue={stats['queue_net_last_hour']:+d} live-margin={stats['live_capacity_margin_last_hour']:+d} "
        f"rss-ingest-dedup={stats['rss_ingest_duplicates_hour']} inflight-waits={stats['dedup_wait_hour']} ETA={eta} | "
        f"429={stats['rate_limits']} timeout={stats['timeouts']} errors={stats['active_errors']}"
    )


def render_text(stats: dict[str, object]) -> str:
    eta = f"{stats['eta_hours']} год" if stats["eta_hours"] is not None else "немає позитивного запасу проти LIVE-потоку"
    return "\n".join([
        "ПЕРВИННИЙ АНАЛІЗ з 05.09.2026 00:00 (Київ)",
        f"Workers:                 {stats['workers_active']}/{stats['workers_configured']} "
        f"(LIVE {stats['live_workers']} + BACKLOG {stats['backlog_workers']})",
        "",
        f"Всього матеріалів:       {stats['total']}",
        f"Оброблено:               {stats['processed']} ({stats['completion_percent']}%)",
        f"Не оброблено:            {stats['pending']}",
        f"  LIVE queue:             {stats['live_pending']}",
        f"  BACKLOG:                {stats['backlog_pending']}",
        f"  очікують retry:         {stats['retry_waiting']}",
        "",
        f"Codex-проаналізовано:     {stats['codex_analyzed']}",
        f"  прийнято:               {stats['accepted_by_analysis']}",
        f"  відхилено аналізом:     {stats['rejected_by_analysis']}",
        f"Без Codex — prefilter:    {stats['fast_skipped']}",
        f"Exact duplicates:         {stats['exact_duplicates']}",
        f"Near duplicates:          {stats['near_duplicates']}",
        f"Зекономлено Codex-запитів:{stats['dedup_saved']:>9}",
        f"Помилок прибрано:         {stats['errors_removed']}",
        f"Активних помилок:         {stats['active_errors']}",
        "",
        "За останню годину — надходження:",
        f"  нових записів у БД:     {stats['db_created_last_hour']}",
        f"  LIVE (свіжі):           {stats['live_ingested_last_hour']}",
        f"  історичний backfill:    {stats['backfill_ingested_last_hour']}",
        f"  без дати публікації:    {stats['undated_ingested_last_hour']}",
        f"  реально опубліковано:   {stats['actually_published_last_hour']}",
        f"  RSS-дублів не створено: {stats['rss_ingest_duplicates_hour']}",
        "",
        "За останню годину — обробка:",
        f"  оброблено:              {stats['processed_last_hour']}",
        f"  Codex:                  {stats['analyzed_last_hour']}",
        f"  prefilter:              {stats['fast_last_hour']}",
        f"  exact duplicates:       {stats['exact_last_hour']}",
        f"  near duplicates:        {stats['near_last_hour']}",
        f"  Codex зекономлено:      {stats['dedup_saved_last_hour']}",
        f"  in-flight dedup waits:  {stats['dedup_wait_hour']}",
        f"  баланс БД-черги:        {stats['queue_net_last_hour']:+d} (оброблено мінус усі нові записи)",
        f"  запас проти LIVE:       {stats['live_capacity_margin_last_hour']:+d}",
        f"  ETA backlog по LIVE:    {eta}",
        f"  429:                    {stats['rate_limits']}",
        f"  timeout:                {stats['timeouts']}",
        f"  інші transient errors:  {stats['transient_errors']}",
        f"  error-dropped:          {stats['error_dropped_hour']}",
        "",
        f"Оновлено: {stats['generated_at']}",
    ])
