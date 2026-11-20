import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db, info_prefilter, telegram_markers
from scripts.sync_primary_info_queue_v1 import dynamic_terms_for_member

CUTOFF = os.getenv("INFO_PREFILTER_CUTOFF", "2026-09-13 21:01:00+00")
VERSION = info_prefilter.RULES_VERSION
BATCH_SIZE = max(100, min(10000, int(os.getenv("INFO_FAST_PREFILTER_BATCH", "5000"))))
COMMIT_EVERY = max(50, min(1000, int(os.getenv("INFO_FAST_PREFILTER_COMMIT_EVERY", "500"))))
LOG_PATH = ROOT / "runtime" / "logs" / "fast_info_prefilter.log"
STATE_PATH = ROOT / "runtime" / "fast_info_prefilter_state.json"


def write_status(payload):
    payload = {"at": datetime.now(timezone.utc).isoformat(), **payload}
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    STATE_PATH.write_text(line, encoding="utf-8")


def json_value(value):
    return json.dumps(value, ensure_ascii=False)


def classify_row(connection, row):
    text = info_prefilter.select_prefilter_text(row["source_text"], row["full_text"], row["feed_text"])
    dynamic_terms = dynamic_terms_for_member(connection, row)
    result = info_prefilter.classify(
        text,
        title=(row["feed_text"] or "")[:800],
        source_title=row["source_title"] or "",
        source_kind=row["source_kind"] or "",
        spread_count=1,
        unique_sources=1,
        cross_platform=False,
        dynamic_terms=dynamic_terms,
        source_groups=row["groups"] or (),
    )
    reasons = tuple(dict.fromkeys((*result.reason_codes, "fast_prefilter_no_spread")))
    return result, reasons


def main():
    started = time.perf_counter()
    connection = db.connect(readonly=False)
    stats = {
        "selected": 0,
        "inserted": 0,
        "conflicts": 0,
        "routes": {},
        "version": VERSION,
    }
    try:
        rows = connection.execute(
            """SELECT d.id,d.source_id,d.source_kind,d.source_title,d.source_url,
                      d.source_text,d.feed_text,d.full_text,d.published_at,d.created_at,
                      COALESCE(s.groups,'[]'::jsonb) groups
                 FROM source_documents d
            LEFT JOIN sources s ON s.id=d.source_id
                WHERE NULLIF(d.created_at,'')::timestamptz >= ?::timestamptz
                  AND NOT EXISTS(
                    SELECT 1 FROM info_prefilter_results_v1 p
                     WHERE p.source_document_id=d.id
                  )
                ORDER BY NULLIF(d.created_at,'')::timestamptz DESC,d.id DESC
                LIMIT ?""",
            (CUTOFF, BATCH_SIZE),
        ).fetchall()
        stats["selected"] = len(rows)

        for index, row in enumerate(rows, 1):
            result, reasons = classify_row(connection, row)
            cursor = connection.execute(
                """INSERT INTO info_prefilter_results_v1(
                       source_document_id,cluster_id,canonical_document_id,route,hard_filter_reason,
                       domain_score,influence_score,spread_score,actor_hits,narrative_hits,
                       hashtag_hits,reason_codes,rules_version,processed_at,updated_at)
                   VALUES(?,NULL,?,?,?,?,?,?,?::jsonb,?::jsonb,?::jsonb,?::jsonb,?,now(),now())
                   ON CONFLICT(source_document_id) DO NOTHING
                   RETURNING source_document_id""",
                (
                    int(row["id"]), int(row["id"]), result.route,
                    result.hard_filter_reason, result.domain_score,
                    result.influence_score, result.spread_score,
                    json_value(result.actor_hits), json_value(result.narrative_hits),
                    json_value(result.hashtag_hits), json_value(reasons), VERSION,
                ),
            )
            inserted = cursor.fetchone() is not None
            if str(row["source_kind"] or "").lower() == "telegram":
                telegram_markers.store_document_markers(connection, row)
            if inserted:
                stats["inserted"] += 1
                stats["routes"][result.route] = stats["routes"].get(result.route, 0) + 1
            else:
                stats["conflicts"] += 1

            if index % COMMIT_EVERY == 0:
                connection.commit()

        connection.commit()
        stats["remaining_since_cutoff"] = int(connection.execute(
            """SELECT count(*) AS n
                 FROM source_documents d
            LEFT JOIN info_prefilter_results_v1 p ON p.source_document_id=d.id
                WHERE NULLIF(d.created_at,'')::timestamptz >= ?::timestamptz
                  AND p.source_document_id IS NULL""",
            (CUTOFF,),
        ).fetchone()["n"])
        stats["status"] = "ok"
        stats["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        write_status(stats)
        print(json.dumps(stats, ensure_ascii=False))
    except Exception as exc:
        connection.rollback()
        write_status({
            "status": "error",
            "error": repr(exc),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        })
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()
