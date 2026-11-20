from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db
from scripts.sync_primary_info_queue_v1 import update_cluster_and_prefilter


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=48)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    connection = db.connect(readonly=False)
    try:
        params = [args.hours]
        limit_sql = ""
        if args.limit > 0:
            limit_sql = " LIMIT ?"
            params.append(args.limit)
        missing_clause = "" if args.force else """
                   AND NOT EXISTS(
                       SELECT 1 FROM info_prefilter_results_v1 pf
                        WHERE pf.source_document_id=cl.canonical_document_id
                   )"""
        rows = connection.execute(
            f"""SELECT cl.id
                  FROM primary_info_clusters_v1 cl
                  JOIN source_documents d ON d.id=cl.canonical_document_id
                 WHERE COALESCE(
                           cl.last_seen_at,
                           NULLIF(d.created_at,'')::timestamptz,
                           NULLIF(d.published_at,'')::timestamptz
                       ) >= now()-(?||' hours')::interval
                 {missing_clause}
                 ORDER BY COALESCE(
                                   cl.last_seen_at,
                                   NULLIF(d.created_at,'')::timestamptz,
                                   NULLIF(d.published_at,'')::timestamptz
                               ) DESC,
                          cl.id DESC
                 {limit_sql}""",
            tuple(params),
        ).fetchall()
        total = len(rows)
        print(f"backfill_candidates={total}", flush=True)
        for index, row in enumerate(rows, 1):
            update_cluster_and_prefilter(connection, int(row["id"]))
            if index % 100 == 0:
                connection.commit()
            if index % 500 == 0 or index == total:
                print(f"processed={index}/{total}", flush=True)
        connection.commit()
    finally:
        connection.close()


if __name__ == "__main__":
    main()

