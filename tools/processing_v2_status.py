"""Read-only status for Processing Engine v2."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect
from app import processing_v2


def main() -> int:
    connection = connect(readonly=True)
    try:
        exists = connection.execute(
            "SELECT to_regclass('public.processing_v2_jobs') AS name"
        ).fetchone()["name"]
        if not exists:
            print("Processing Engine v2 schema is not installed.")
            return 0

        rows = processing_v2.queue_stats(connection)
        if not rows:
            print("Processing Engine v2 queue is empty.")
            return 0

        print(f"Pipeline: {processing_v2.PIPELINE_VERSION}")
        total = 0
        for row in rows:
            jobs = int(row["jobs"])
            total += jobs
            print(
                f"{row['stage']:8} {row['status']:12} "
                f"{jobs:8}  stale={int(row['stale_processing'] or 0)} "
                f"oldest={row['oldest_created_at']}"
            )
        print(f"TOTAL {total}")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
