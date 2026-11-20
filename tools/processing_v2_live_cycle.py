"""One-shot recent-first cycle for Processing Engine v2.

Dry-run by default. The cycle is deliberately split:
- --apply-local may seed recent originals and run only no-AI stages.
- --apply-ai additionally permits bounded Collector AI screening and requires
  an explicit --max-used-percent guard.

The live raw collector remains independent and is never restarted here.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import ROOT, connect
from app import processing_v2

PREFILTER_WORKER = ROOT / "tools" / "processing_v2_prefilter_worker.py"
HYDRATE_WORKER = ROOT / "tools" / "processing_v2_hydrate_worker.py"
GUARDED_AI = ROOT / "tools" / "processing_v2_guarded_run.py"


def _run(command: list[str]) -> None:
    completed = subprocess.run(command, cwd=ROOT, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"child process exited with code {completed.returncode}: "
            + " ".join(command)
        )


def _missing_recent_count() -> int:
    connection = connect(readonly=True)
    try:
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
                         AND j.stage='prefilter'
                  )""",
            (processing_v2.PIPELINE_VERSION,),
        ).fetchone()
        return int(row["n"] or 0)
    finally:
        connection.close()


def _queue_counts() -> dict[tuple[str, str], int]:
    connection = connect(readonly=True)
    try:
        rows = connection.execute(
            """SELECT stage,status,count(*) AS n
                 FROM processing_v2_jobs
                WHERE pipeline_version=?
                GROUP BY stage,status""",
            (processing_v2.PIPELINE_VERSION,),
        ).fetchall()
        return {
            (str(row["stage"]), str(row["status"])): int(row["n"])
            for row in rows
        }
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recent-limit", type=int, default=100)
    parser.add_argument("--apply-local", action="store_true")
    parser.add_argument("--apply-ai", action="store_true")
    parser.add_argument("--ai-batch-size", type=int, default=20)
    parser.add_argument("--ai-max-batches", type=int, default=1)
    parser.add_argument("--max-used-percent", type=int)
    args = parser.parse_args()

    recent_limit = max(1, min(int(args.recent_limit), 5000))
    ai_batch_size = max(1, min(int(args.ai_batch_size), 20))
    ai_max_batches = max(1, min(int(args.ai_max_batches), 100))

    if args.apply_ai and not args.apply_local:
        raise SystemExit("--apply-ai requires --apply-local")
    if args.apply_ai and args.max_used_percent is None:
        raise SystemExit(
            "--apply-ai requires an explicit --max-used-percent threshold"
        )

    print("Processing Engine v2 — RECENT LIVE CYCLE")
    print("Raw collector: untouched")
    print(f"Pipeline: {processing_v2.PIPELINE_VERSION}")
    print(f"Recent seed cap: {recent_limit}")
    print(f"Unqueued originals in archive: {_missing_recent_count()}")

    before = _queue_counts()
    print("Queue before:")
    if before:
        for key in sorted(before):
            print(f"  {key[0]:10} {key[1]:12} {before[key]}")
    else:
        print("  empty")

    if not args.apply_local:
        print("DB writes: 0")
        print("AI calls: 0")
        print(
            "Use --apply-local to seed newest originals and run only "
            "prefilter/hydration."
        )
        return 0

    connection = connect()
    try:
        seeded = processing_v2.seed_recent(
            connection,
            limit=recent_limit,
            priority=10,
        )
    finally:
        connection.close()
    print(f"Seeded newest originals: {seeded}")

    if seeded > 0:
        _run(
            [
                sys.executable,
                str(PREFILTER_WORKER),
                "--apply",
                "--max-docs",
                str(seeded),
                "--batch-size",
                str(min(seeded, 500)),
            ]
        )

    counts = _queue_counts()
    hydration_ready = (
        counts.get(("hydrate", "pending"), 0)
        + counts.get(("hydrate", "retry"), 0)
    )
    if hydration_ready > 0:
        # The hydrator is already hard-capped at 100 docs per invocation, so
        # process in bounded chunks rather than widening its safety limit.
        remaining = hydration_ready
        while remaining > 0:
            amount = min(100, remaining)
            _run(
                [
                    sys.executable,
                    str(HYDRATE_WORKER),
                    "--apply",
                    "--max-docs",
                    str(amount),
                    "--batch-size",
                    "5",
                    "--delay",
                    "0.05",
                ]
            )
            new_counts = _queue_counts()
            new_ready = (
                new_counts.get(("hydrate", "pending"), 0)
                + new_counts.get(("hydrate", "retry"), 0)
            )
            if new_ready >= remaining:
                break
            remaining = new_ready

    if args.apply_ai:
        _run(
            [
                sys.executable,
                str(GUARDED_AI),
                "--apply",
                "--batch-size",
                str(ai_batch_size),
                "--max-batches",
                str(ai_max_batches),
                "--max-used-percent",
                str(int(args.max_used_percent)),
                "--reasoning",
                "low",
                "--verbosity",
                "low",
            ]
        )
    else:
        print("AI calls: 0")
        print(
            "Local stages completed. Add --apply-ai plus an explicit "
            "--max-used-percent to permit Collector AI screening."
        )

    after = _queue_counts()
    print("Queue after:")
    for key in sorted(after):
        print(f"  {key[0]:10} {key[1]:12} {after[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
