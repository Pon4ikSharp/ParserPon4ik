"""One-shot deterministic prefilter worker for Processing Engine v2.

No AI calls. Writes only processing_v2_* tables. source_documents is read-only.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect
from app import prefilter_v2, processing_v2


def _exact_duplicate(connection, document_id: int, content_hash: str):
    if not content_hash:
        return None
    row = connection.execute(
        """SELECT min(id) AS canonical_id,count(*) AS n
             FROM source_documents
            WHERE content_hash=?""",
        (content_hash,),
    ).fetchone()
    if not row or int(row["n"] or 0) <= 1:
        return None
    canonical_id = int(row["canonical_id"])
    if canonical_id == document_id:
        return None
    return canonical_id, int(row["n"])


def _process_batch(connection, jobs, worker_name: str) -> dict[str, int]:
    prepared = []
    counts: dict[str, int] = {}

    for job in jobs:
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
                commit=False,
            )
            counts["missing"] = counts.get("missing", 0) + 1
            continue

        basis, text = prefilter_v2.choose_input(document)
        result = prefilter_v2.classify(text).as_dict()
        result["document_id"] = int(document["id"])
        result["job"] = job
        result["basis"] = basis
        result["source_kind"] = document["source_kind"]
        result["content_hash"] = document["content_hash"] or ""
        result["document"] = document

        if result["decision"] == "pass":
            duplicate = _exact_duplicate(
                connection,
                int(document["id"]),
                result["content_hash"],
            )
            if duplicate:
                result["decision"] = "duplicate_exact"
                result["reason"] = "same_content_hash"
                result["canonical_document_id"] = duplicate[0]
                result["duplicate_count_archive"] = duplicate[1]

        prepared.append(result)

    # Near-duplicate detection is bounded to this claimed batch. This is useful
    # for the benchmark and intentionally does not pretend to be a global ANN
    # index. Exact duplicate detection above is archive-wide.
    pass_items = [item for item in prepared if item["decision"] == "pass"]
    near_map = prefilter_v2.find_near_duplicates(pass_items, threshold=0.90)
    for duplicate_idx, (canonical_idx, similarity) in near_map.items():
        duplicate = pass_items[duplicate_idx]
        canonical = pass_items[canonical_idx]
        duplicate["decision"] = "duplicate_near"
        duplicate["reason"] = "simhash_near_duplicate_in_batch"
        duplicate["canonical_document_id"] = canonical["document_id"]
        duplicate["similarity"] = similarity

    for item in prepared:
        job = item["job"]
        result = {
            key: item[key]
            for key in (
                "decision",
                "reason",
                "char_count",
                "word_count",
                "url_count",
                "exact_key",
                "fingerprint64",
            )
        }
        canonical_id = item.get("canonical_document_id")
        similarity = item.get("similarity")
        details = {
            "source_kind": item["source_kind"],
            "duplicate_count_archive": item.get("duplicate_count_archive", 0),
            "near_duplicate_scope": "claimed_batch",
        }
        processing_v2.store_prefilter_result(
            connection,
            source_document_id=item["document_id"],
            pipeline_version=str(job["pipeline_version"]),
            source_hash=str(job["source_hash"]),
            input_basis=item["basis"],
            result=result,
            canonical_document_id=canonical_id,
            similarity=similarity,
            details=details,
            commit=False,
        )
        processing_v2.complete_job(
            connection,
            int(job["id"]),
            worker=worker_name,
            detail={
                "decision": item["decision"],
                "reason": item["reason"],
                "canonical_document_id": canonical_id,
            },
            commit=False,
        )

        if item["decision"] == "pass":
            processing_v2.route_passed_prefilter(
                connection,
                item["document_id"],
                pipeline_version=str(job["pipeline_version"]),
                priority=int(job["priority"]),
                commit=False,
            )

        counts[item["decision"]] = counts.get(item["decision"], 0) + 1

    connection.commit()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Claim and process prefilter jobs. Without this flag no writes occur.",
    )
    parser.add_argument("--max-docs", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--worker-name", default="prefilter-v2")
    args = parser.parse_args()

    max_docs = max(1, min(int(args.max_docs), 1000))
    batch_size = max(1, min(int(args.batch_size), 500))
    worker_name = args.worker_name.strip() or "prefilter-v2"

    connection = connect(readonly=not args.apply)
    try:
        exists = connection.execute(
            "SELECT to_regclass('public.processing_v2_jobs') AS name"
        ).fetchone()["name"]
        if not exists:
            print("Processing Engine v2 schema is not installed.")
            return 1

        if not args.apply:
            row = connection.execute(
                """SELECT count(*) AS n
                     FROM processing_v2_jobs
                    WHERE pipeline_version=?
                      AND stage='prefilter'
                      AND status IN ('pending','retry')
                      AND next_attempt_at<=now()""",
                (processing_v2.PIPELINE_VERSION,),
            ).fetchone()
            print("Processing Engine v2 — PREFILTER worker preview")
            print("AI calls: 0")
            print("DB writes: 0")
            print(f"Ready prefilter jobs: {int(row['n'] or 0)}")
            print(f"Hard max docs:        {max_docs}")
            return 0

        total = 0
        totals: dict[str, int] = {}
        while total < max_docs:
            amount = min(batch_size, max_docs - total)
            jobs = processing_v2.claim_batch(
                connection,
                worker_name,
                stage="prefilter",
                limit=amount,
            )
            if not jobs:
                break
            result = _process_batch(connection, jobs, worker_name)
            processed_now = sum(result.values())
            total += processed_now
            for key, value in result.items():
                totals[key] = totals.get(key, 0) + value
            print(f"Processed {total}/{max_docs}")

        print()
        print("PREFILTER RESULT")
        print(f"Processed: {total}")
        for key in sorted(totals):
            print(f"  {key:20} {totals[key]}")
        print("AI calls: 0")
        print("source_documents writes: 0")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
