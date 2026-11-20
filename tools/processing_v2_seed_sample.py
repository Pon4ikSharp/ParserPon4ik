"""Seed a bounded mixed sample for Processing Engine v2.

This tool is intended for controlled benchmarks. It inserts only prefilter jobs
into processing_v2_jobs and never changes source_documents.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect
from app import processing_v2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram", type=int, default=50)
    parser.add_argument("--rss", type=int, default=50)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually insert the bounded sample jobs.",
    )
    args = parser.parse_args()

    telegram_n = max(0, min(int(args.telegram), 500))
    rss_n = max(0, min(int(args.rss), 500))
    if telegram_n + rss_n <= 0:
        raise SystemExit("Sample size must be greater than zero")

    connection = connect(readonly=not args.apply)
    try:
        exists = connection.execute(
            "SELECT to_regclass('public.processing_v2_jobs') AS name"
        ).fetchone()["name"]
        if not exists:
            print("Processing Engine v2 schema is not installed.")
            return 1

        selected = []
        for kind, amount in (("telegram", telegram_n), ("rss", rss_n)):
            if amount <= 0:
                continue
            rows = connection.execute(
                """SELECT id,source_kind,source_title,content_hash,created_at
                     FROM source_documents d
                    WHERE source_kind=?
                      AND length(trim(coalesce(content_hash,'')))>0
                      AND NOT EXISTS (
                          SELECT 1
                            FROM processing_v2_jobs j
                           WHERE j.source_document_id=d.id
                             AND j.pipeline_version=?
                             AND j.source_hash=d.content_hash
                             AND j.stage='prefilter'
                      )
                    ORDER BY id DESC
                    LIMIT ?""",
                (kind, processing_v2.PIPELINE_VERSION, amount),
            ).fetchall()
            selected.extend(rows)

        by_kind = {
            "telegram": sum(1 for row in selected if row["source_kind"] == "telegram"),
            "rss": sum(1 for row in selected if row["source_kind"] == "rss"),
        }

        print("Processing Engine v2 — bounded sample seed")
        print(f"Pipeline: {processing_v2.PIPELINE_VERSION}")
        print(f"Selected: {len(selected)}")
        print(f"  telegram: {by_kind['telegram']}")
        print(f"  rss:      {by_kind['rss']}")

        if not args.apply:
            print("DB writes: 0")
            print("Add --apply to insert these prefilter jobs.")
            return 0

        inserted = 0
        for row in selected:
            before = connection.execute(
                """SELECT id FROM processing_v2_jobs
                    WHERE source_document_id=? AND pipeline_version=?
                      AND source_hash=? AND stage='prefilter'""",
                (
                    int(row["id"]),
                    processing_v2.PIPELINE_VERSION,
                    row["content_hash"],
                ),
            ).fetchone()
            if before:
                continue
            job_id = processing_v2.enqueue_document(
                connection,
                int(row["id"]),
                pipeline_version=processing_v2.PIPELINE_VERSION,
                stage="prefilter",
                commit=False,
            )
            if job_id is not None:
                inserted += 1

        connection.commit()
        print(f"Inserted prefilter jobs: {inserted}")
        print("AI calls: 0")
        print("source_documents writes: 0")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
