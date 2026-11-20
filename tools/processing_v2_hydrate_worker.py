"""One-shot no-AI hydration worker for Processing Engine v2.

The worker only writes processing_v2_* tables. It never rewrites
source_documents and never calls an AI model.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect
from app import hydration_v2, processing_v2


def process_claimed_job(connection, job: dict, *,
                        proxy_url: str | None,
                        proxy_domains: tuple[str, ...],
                        worker_name: str) -> str:
    """Fetch and persist one already-claimed hydration job."""
    document = connection.execute(
        """SELECT id,source_kind,source_title,source_url,source_text,
                  feed_text,full_text,content_status,content_hash
             FROM source_documents
            WHERE id=?""",
        (job["source_document_id"],),
    ).fetchone()
    if not document:
        processing_v2.fail_job(
            connection,
            int(job["id"]),
            "source document no longer exists",
            worker=worker_name,
            retry_delay_seconds=0,
        )
        return "missing"

    outcome = hydration_v2.fetch_document(
        document,
        proxy_url=proxy_url,
        proxy_domains=proxy_domains,
    )

    try:
        processing_v2.store_hydration_result(
            connection,
            source_document_id=int(document["id"]),
            pipeline_version=str(job["pipeline_version"]),
            source_hash=str(job["source_hash"]),
            outcome=outcome,
            commit=False,
        )

        if hydration_v2.should_retry(outcome):
            status = processing_v2.fail_job(
                connection,
                int(job["id"]),
                outcome.get("error") or "hydration fetch failed",
                worker=worker_name,
                retry_delay_seconds=120,
                detail={
                    "hydration_status": outcome["status"],
                    "duration_ms": outcome["duration_ms"],
                },
                commit=False,
            )
            connection.commit()
            return status

        processing_v2.complete_job(
            connection,
            int(job["id"]),
            worker=worker_name,
            detail={
                "hydration_status": outcome["status"],
                "full_text_chars": outcome["full_text_chars"],
                "duration_ms": outcome["duration_ms"],
                "used_proxy": outcome["used_proxy"],
            },
            commit=False,
        )
        processing_v2.enqueue_analysis_after_hydration(
            connection,
            int(document["id"]),
            pipeline_version=str(job["pipeline_version"]),
            priority=int(job["priority"]),
            commit=False,
        )
        connection.commit()
        return "completed"
    except Exception:
        connection.rollback()
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Claim and process hydration jobs. Without this flag no writes occur.",
    )
    parser.add_argument("--max-docs", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--delay", type=float, default=0.25)
    parser.add_argument("--worker-name", default="")
    args = parser.parse_args()

    max_docs = max(1, min(int(args.max_docs), 100))
    batch_size = max(1, min(int(args.batch_size), 10))
    delay = max(0.0, float(args.delay))
    worker_name = args.worker_name.strip() or (
        f"hydration-v2:{socket.gethostname()}:{os.getpid()}"
    )

    connection = connect(readonly=not args.apply)
    try:
        exists = connection.execute(
            "SELECT to_regclass('public.processing_v2_jobs') AS name"
        ).fetchone()["name"]
        if not exists:
            print("Processing Engine v2 schema is not installed.")
            print("No changes made.")
            return 0

        if not args.apply:
            row = connection.execute(
                """SELECT count(*) AS n
                     FROM processing_v2_jobs
                    WHERE pipeline_version=?
                      AND stage='hydrate'
                      AND status IN ('pending','retry')
                      AND next_attempt_at<=now()""",
                (processing_v2.PIPELINE_VERSION,),
            ).fetchone()
            print("Processing Engine v2 — HYDRATION worker preview")
            print("AI calls: 0")
            print("DB writes: 0")
            print(f"Ready hydration jobs: {int(row['n'] or 0)}")
            print(f"Max docs if applied:   {max_docs}")
            print("Add --apply to run this bounded one-shot worker.")
            return 0

        proxy_url, proxy_domains = hydration_v2.configured_proxy()
        processed = 0
        outcomes: dict[str, int] = {}

        print("Processing Engine v2 — HYDRATION worker")
        print("AI calls: 0")
        print("source_documents writes: 0")
        print(f"Hard max docs: {max_docs}")
        print(f"Batch size:    {batch_size}")
        print()

        while processed < max_docs:
            request_count = min(batch_size, max_docs - processed)
            jobs = processing_v2.claim_batch(
                connection,
                worker_name,
                stage="hydrate",
                limit=request_count,
            )
            if not jobs:
                break

            for job in jobs:
                status = process_claimed_job(
                    connection,
                    job,
                    proxy_url=proxy_url,
                    proxy_domains=proxy_domains,
                    worker_name=worker_name,
                )
                processed += 1
                outcomes[status] = outcomes.get(status, 0) + 1
                print(
                    f"{processed}/{max_docs} "
                    f"job={job['id']} doc={job['source_document_id']} "
                    f"result={status}"
                )
                if delay:
                    time.sleep(delay)

        print()
        print(f"Processed: {processed}")
        for key in sorted(outcomes):
            print(f"  {key:12} {outcomes[key]}")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
