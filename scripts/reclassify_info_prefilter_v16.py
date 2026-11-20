from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db, info_prefilter

SCOPE_START = "2026-09-21 00:00:00+03"


def json_value(value):
    return json.dumps(value, ensure_ascii=False)


def load_rows(connection):
    return connection.execute(
        """SELECT d.id,d.source_kind,d.source_title,d.feed_text,d.source_text,d.full_text,
                  COALESCE(s.groups,'[]'::jsonb) groups,p.route old_route
             FROM info_prefilter_results_v1 p
             JOIN source_documents d ON d.id=p.source_document_id
        LEFT JOIN sources s ON s.id=d.source_id
            WHERE p.route='HOLD'
              AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                           NULLIF(d.created_at,'')::timestamptz)>=?::timestamptz
            ORDER BY d.id""",
        (SCOPE_START,),
    ).fetchall()


def classify_row(row):
    text = info_prefilter.select_prefilter_text(
        row["source_text"], row["full_text"], row["feed_text"]
    )
    return info_prefilter.classify(
        text,
        title=(row["feed_text"] or "")[:800],
        source_title=row["source_title"] or "",
        source_kind=row["source_kind"] or "",
        source_groups=row["groups"] or (),
    )


def upgraded(result):
    if result.route == "ANALYZE":
        return bool(result.narrative_hits)
    if result.route == "MUST_ANALYZE":
        return bool(result.narrative_hits)
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    connection = db.connect(readonly=not args.commit)
    rows = load_rows(connection)
    stats = Counter()
    families = Counter()
    samples = []
    try:
        for row in rows:
            stats["tested"] += 1
            result = classify_row(row)
            if not upgraded(result):
                continue
            stats["upgraded"] += 1
            stats[f"route:{result.route}"] += 1
            for family in result.narrative_hits:
                families[family] += 1
            if len(samples) < 40:
                samples.append((
                    int(row["id"]), row["source_title"], result.route,
                    result.domain_score, result.influence_score,
                    list(result.narrative_hits),
                ))
            if not args.commit:
                continue
            reasons = tuple(dict.fromkeys((
                *result.reason_codes,
                "reclassified_from_hold:v16",
            )))
            connection.execute(
                """UPDATE info_prefilter_results_v1
                      SET route=?,hard_filter_reason=?,domain_score=?,influence_score=?,
                          spread_score=?,actor_hits=?::jsonb,narrative_hits=?::jsonb,
                          hashtag_hits=?::jsonb,reason_codes=?::jsonb,rules_version=?,updated_at=now()
                    WHERE source_document_id=? AND route='HOLD'""",
                (
                    result.route, result.hard_filter_reason,
                    result.domain_score, result.influence_score,
                    result.spread_score,
                    json_value(result.actor_hits),
                    json_value(result.narrative_hits),
                    json_value(result.hashtag_hits),
                    json_value(reasons), info_prefilter.RULES_VERSION,
                    int(row["id"]),
                ),
            )
        if args.commit:
            connection.commit()
        print(json.dumps({
            "commit": args.commit,
            "rules_version": info_prefilter.RULES_VERSION,
            "scope_start": SCOPE_START,
            "stats": dict(stats),
            "families": dict(families),
            "samples": samples,
        }, ensure_ascii=False, indent=2))
    except Exception:
        if args.commit:
            connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()
