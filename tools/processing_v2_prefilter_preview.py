"""Read-only benchmark of the deterministic Processing Engine v2 prefilter.

No rows are inserted/updated/deleted. No AI model is called.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
from collections import Counter

from app.db import connect
from app import prefilter_v2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--near-threshold",
        type=float,
        default=0.90,
        help="SimHash similarity threshold for bounded sample near-duplicates.",
    )
    args = parser.parse_args()
    limit = max(1, min(int(args.limit), 5000))

    connection = connect(readonly=True)
    try:
        rows = connection.execute(
            """SELECT id,source_kind,source_title,content_hash,
                      source_text,feed_text,full_text,content_status,
                      published_at,created_at
                 FROM source_documents
                ORDER BY id DESC
                LIMIT ?""",
            (limit,),
        ).fetchall()

        prepared = []
        for row in rows:
            basis, text = prefilter_v2.choose_input(row)
            result = prefilter_v2.classify(text).as_dict()
            result.update(
                {
                    "id": int(row["id"]),
                    "source_kind": row["source_kind"],
                    "source_title": row["source_title"],
                    "content_hash": row["content_hash"] or "",
                    "input_basis": basis,
                    "content_status": row["content_status"],
                }
            )
            prepared.append(result)

        # Existing archive-wide exact duplicate information for the sampled
        # hashes. This uses a bounded IN query and remains read-only.
        hashes = sorted({x["content_hash"] for x in prepared if x["content_hash"]})
        duplicate_meta = {}
        if hashes:
            placeholders = ",".join("?" for _ in hashes)
            dup_rows = connection.execute(
                f"""SELECT content_hash,count(*) AS n,min(id) AS canonical_id
                      FROM source_documents
                     WHERE content_hash IN ({placeholders})
                     GROUP BY content_hash
                    HAVING count(*)>1""",
                tuple(hashes),
            ).fetchall()
            duplicate_meta = {
                row["content_hash"]: (int(row["n"]), int(row["canonical_id"]))
                for row in dup_rows
            }

        exact_duplicates = 0
        for item in prepared:
            meta = duplicate_meta.get(item["content_hash"])
            if (
                item["decision"] == "pass"
                and meta
                and item["id"] != meta[1]
            ):
                item["decision"] = "duplicate_exact"
                item["reason"] = "same_content_hash"
                item["canonical_id"] = meta[1]
                item["duplicate_count_archive"] = meta[0]
                exact_duplicates += 1

        # Near-duplicate detection is deliberately bounded to this benchmark
        # sample. Full-scale candidate bucketing will be a separate step.
        pass_items = [x for x in prepared if x["decision"] == "pass"]
        near_map = prefilter_v2.find_near_duplicates(
            pass_items, threshold=float(args.near_threshold)
        )
        near_duplicates = 0
        for duplicate_idx, (canonical_idx, similarity) in near_map.items():
            duplicate = pass_items[duplicate_idx]
            canonical = pass_items[canonical_idx]
            duplicate["decision"] = "duplicate_near"
            duplicate["reason"] = "simhash_near_duplicate_in_sample"
            duplicate["canonical_id"] = canonical["id"]
            duplicate["similarity"] = round(similarity, 4)
            near_duplicates += 1

        decision_counts = Counter(x["decision"] for x in prepared)
        reason_counts = Counter(x["reason"] for x in prepared)

        ai_candidates = [
            x for x in prepared
            if x["decision"] == "pass"
        ]
        hydrate_candidates = [
            x for x in ai_candidates
            if x["source_kind"] == "rss"
            and x["input_basis"] != "full_text"
            and x["content_status"] != "paywall"
        ]
        direct_analysis = len(ai_candidates) - len(hydrate_candidates)

        print("Processing Engine v2 — LOCAL PREFILTER preview")
        print("AI calls: 0")
        print("DB writes: 0")
        print(f"Sample: {len(prepared)} latest source_documents")
        print()
        print("DECISIONS")
        for key in sorted(decision_counts):
            print(f"  {key:20} {decision_counts[key]}")
        print()
        print("REASONS")
        for key, value in reason_counts.most_common():
            print(f"  {key:36} {value}")
        print()
        print("ROUTING AFTER PREFILTER")
        print(f"  AI/hydration candidates: {len(ai_candidates)}")
        print(f"  need hydrate first:       {len(hydrate_candidates)}")
        print(f"  direct analysis:          {direct_analysis}")
        print(f"  exact duplicate saved:    {exact_duplicates}")
        print(f"  near duplicate saved:     {near_duplicates}")
        saved = len(prepared) - len(ai_candidates)
        pct = (saved / len(prepared) * 100.0) if prepared else 0.0
        print(f"  not sent to AI:           {saved} ({pct:.1f}%)")
        print()
        print("NON-PASS SAMPLE IDS")
        shown = 0
        for item in prepared:
            if item["decision"] == "pass":
                continue
            extra = ""
            if item.get("canonical_id"):
                extra += f" canonical={item['canonical_id']}"
            if item.get("similarity"):
                extra += f" sim={item['similarity']}"
            print(
                f"  id={item['id']} {item['decision']} "
                f"reason={item['reason']} source={item['source_kind']}"
                f"{extra}"
            )
            shown += 1
            if shown >= 20:
                break

        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
