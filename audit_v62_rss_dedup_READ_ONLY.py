from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.db import connect
from app import primary_dedup


def main():
    db = connect()
    try:
        # Force the PostgreSQL transaction to be read-only. Any accidental write
        # in this audit would fail at the database level.
        try:
            db.raw.execute("BEGIN READ ONLY")
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            db.raw.execute("BEGIN")
            db.raw.execute("SET TRANSACTION READ ONLY")

        docs = db.execute(
            """SELECT id, source_id, source_title, source_url, source_text, full_text,
                      published_at, created_at
                 FROM source_documents
                WHERE source_kind='rss' AND source_id IS NOT NULL
                ORDER BY id"""
        ).fetchall()

        keys = db.execute(
            """SELECT id, source_id, source_document_id, canonical_url, normalized_hash
                 FROM rss_ingest_keys
                ORDER BY id"""
        ).fetchall()

        info_count = db.execute("SELECT count(*) AS n FROM info_influence").fetchone()["n"]
        source_doc_count = db.execute("SELECT count(*) AS n FROM source_documents").fetchone()["n"]
        rss_doc_count = len(docs)
        key_count = len(keys)
        keyed_docs = {int(r["source_document_id"]) for r in keys}

        by_url = defaultdict(list)
        by_hash = defaultdict(list)
        cross_source_url = defaultdict(list)
        eligible = 0
        no_key_material = 0

        doc_meta = {}
        for row in docs:
            doc_id = int(row["id"])
            source_id = int(row["source_id"])
            title = row["source_title"] or ""
            url = primary_dedup.canonicalize_url(row["source_url"] or "")
            text = (row["full_text"] or row["source_text"] or "").strip()
            n_hash = primary_dedup.rss_ingest_normalized_hash(text) if text else ""

            doc_meta[doc_id] = {
                "source_id": source_id,
                "title": title,
                "url": url,
                "hash": n_hash,
                "published_at": row["published_at"],
            }

            if url or n_hash:
                eligible += 1
            else:
                no_key_material += 1

            if url:
                by_url[(source_id, url)].append(doc_id)
                cross_source_url[url].append((source_id, doc_id))
            if n_hash:
                by_hash[(source_id, n_hash)].append(doc_id)

        url_groups = {k: v for k, v in by_url.items() if len(v) > 1}
        hash_groups = {k: v for k, v in by_hash.items() if len(v) > 1}

        duplicate_candidate_ids = set()
        for ids in url_groups.values():
            duplicate_candidate_ids.update(ids[1:])
        for ids in hash_groups.values():
            duplicate_candidate_ids.update(ids[1:])

        cross_source_groups = {}
        for url, items in cross_source_url.items():
            source_ids = {sid for sid, _ in items}
            if len(source_ids) > 1:
                cross_source_groups[url] = items

        # Existing key collisions: how many source_documents are eligible but do
        # not have their own key row because an older same-source key won.
        eligible_without_own_key = [
            int(r["id"])
            for r in docs
            if int(r["id"]) not in keyed_docs and (
                doc_meta[int(r["id"])]["url"] or doc_meta[int(r["id"])]["hash"]
            )
        ]

        print("=" * 78)
        print("V6.2 RSS DEDUP READ-ONLY AUDIT")
        print("NO DATABASE WRITES ARE PERFORMED")
        print("=" * 78)
        print()
        print(f"source_documents total:                {int(source_doc_count)}")
        print(f"info_influence total:                  {int(info_count)}")
        print(f"RSS source_documents:                  {rss_doc_count}")
        print(f"RSS docs eligible for ingest key:      {eligible}")
        print(f"rss_ingest_keys rows:                  {key_count}")
        print(f"distinct RSS docs owning a key:        {len(keyed_docs)}")
        print(f"RSS docs with no key material:         {no_key_material}")
        print()
        print("ACTUAL SAME-SOURCE DUPLICATE CANDIDATES UNDER V6.2 RULES")
        print(f"same canonical URL groups:             {len(url_groups)}")
        print(f"same normalized-text hash groups:      {len(hash_groups)}")
        print(f"unique duplicate-candidate documents:  {len(duplicate_candidate_ids)}")
        print(f"eligible docs without own key row:     {len(eligible_without_own_key)}")
        print()
        print("CROSS-SOURCE SAME-URL GROUPS (should NOT be suppressed by source_id rule)")
        print(f"cross-source canonical URL groups:     {len(cross_source_groups)}")
        print()
        print("IMPORTANT:")
        print(f"V62_RSS_INGEST_KEYS_BACKFILLED would correspond to key rows inserted,")
        print(f"NOT to duplicate count. Current key rows: {key_count}.")
        print()

        def show_groups(label, groups, limit=15):
            print("-" * 78)
            print(label)
            shown = 0
            for key, ids in sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True):
                if shown >= limit:
                    break
                source_id = key[0]
                print(f"\nsource_id={source_id}  copies={len(ids)}")
                for doc_id in ids[:8]:
                    m = doc_meta[doc_id]
                    print(
                        f"  id={doc_id} | {m['title'][:55]} | "
                        f"published={m['published_at']} | url={m['url'][:100]}"
                    )
                shown += 1
            if shown == 0:
                print("  none")

        show_groups("TOP SAME-CANONICAL-URL GROUPS", url_groups)
        show_groups("TOP SAME-NORMALIZED-TEXT GROUPS", hash_groups)

        print()
        print("=" * 78)
        print("AUDIT COMPLETE. Transaction will be rolled back.")
        print("=" * 78)

    finally:
        try:
            db.rollback()
        except Exception:
            pass
        db.close()


if __name__ == "__main__":
    main()
