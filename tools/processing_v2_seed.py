"""Seed Processing Engine v2 jobs from source_documents.

This is deliberately a separate operator action. It does not start workers.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse

from app.db import connect
from app import processing_v2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually insert missing jobs. Without this flag nothing is changed.",
    )
    args = parser.parse_args()

    connection = connect(readonly=not args.apply)
    try:
        tables = connection.execute(
            "SELECT to_regclass('public.processing_v2_jobs') AS name"
        ).fetchone()["name"]
        if not tables:
            raise RuntimeError(
                "Processing Engine v2 schema is not installed. "
                "Run tools/processing_v2_migrate.py --apply first."
            )

        if not args.apply:
            row = connection.execute(
                """SELECT count(*) AS n
                     FROM source_documents d
                    WHERE length(trim(coalesce(d.content_hash,'')))>0
                      AND NOT EXISTS (
                          SELECT 1
                            FROM processing_v2_jobs j
                           WHERE j.source_document_id=d.id
                             AND j.pipeline_version=?
                             AND j.source_hash=d.content_hash
                      )""",
                (processing_v2.PIPELINE_VERSION,),
            ).fetchone()
            print(f"Missing documents for {processing_v2.PIPELINE_VERSION}: {int(row['n'])}")
            print("No changes made. Add --apply to seed jobs.")
            return 0

        count = processing_v2.seed_missing(
            connection,
            limit=max(1, args.limit),
        )
        print(f"Seeded jobs: {count}")
        print("No workers were started.")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
