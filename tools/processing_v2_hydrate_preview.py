"""Read-only network preview for Processing Engine v2 hydration.

Fetches public article pages but performs no database writes and no AI calls.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect
from app import hydration_v2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument(
        "--id",
        dest="ids",
        action="append",
        type=int,
        default=[],
        help="Specific source_document id; may be repeated.",
    )
    args = parser.parse_args()
    limit = max(1, min(int(args.limit), 20))

    connection = connect(readonly=True)
    try:
        if args.ids:
            placeholders = ",".join("?" for _ in args.ids)
            rows = connection.execute(
                f"""SELECT id,source_kind,source_title,source_url,source_text,
                           feed_text,full_text,content_status,content_hash
                      FROM source_documents
                     WHERE id IN ({placeholders})
                     ORDER BY id DESC""",
                tuple(args.ids),
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT id,source_kind,source_title,source_url,source_text,
                          feed_text,full_text,content_status,content_hash
                     FROM source_documents
                    WHERE source_kind IN ('rss','web')
                      AND length(trim(coalesce(source_url,'')))>0
                      AND length(trim(coalesce(full_text,'')))=0
                    ORDER BY id DESC
                    LIMIT ?""",
                (limit,),
            ).fetchall()
    finally:
        connection.close()

    proxy_url, proxy_domains = hydration_v2.configured_proxy()
    print("Processing Engine v2 — HYDRATION preview")
    print("AI calls: 0")
    print("DB writes: 0")
    print(f"Documents: {len(rows)}")
    print(f"Proxy enabled: {'yes' if proxy_url else 'no'}")
    print(f"Proxy domain allow-list: {len(proxy_domains)}")
    print()

    counts: dict[str, int] = {}
    total_ms = 0
    total_chars = 0

    for row in rows:
        outcome = hydration_v2.fetch_document(
            row,
            proxy_url=proxy_url,
            proxy_domains=proxy_domains,
        )
        status = outcome["status"]
        counts[status] = counts.get(status, 0) + 1
        total_ms += int(outcome["duration_ms"])
        total_chars += int(outcome["full_text_chars"])
        host = urlsplit(row["source_url"]).hostname or ""
        error = outcome["error"]
        error_short = error[:120] if error else ""
        print(
            f"id={row['id']} host={host} status={status} "
            f"text={outcome['full_text_chars']} html={outcome['raw_html_chars']} "
            f"ms={outcome['duration_ms']} proxy={'yes' if outcome['used_proxy'] else 'no'}"
        )
        if error_short:
            print(f"  error={error_short}")

    print()
    print("SUMMARY")
    for key in sorted(counts):
        print(f"  {key:12} {counts[key]}")
    print(f"  full text chars: {total_chars}")
    print(f"  total fetch ms:  {total_ms}")
    if rows:
        print(f"  avg fetch ms:    {total_ms // len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
