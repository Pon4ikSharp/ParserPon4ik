from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db, hydration_v2
from scripts.sync_primary_info_queue_v1 import source_priority, update_cluster_and_prefilter

DEFAULT_BATCH = max(1, min(50, int(os.getenv("INFO_HYDRATION_BATCH", "10"))))
DEFAULT_TIMEOUT = max(5.0, min(30.0, float(os.getenv("INFO_HYDRATION_TIMEOUT", "12"))))


def optional_proxy():
    """Use existing proxy credentials only inside this worker.

    The collector's global PROXY_ENABLED setting is not changed.
    """
    load_dotenv(ROOT / ".env")
    old = os.environ.get("PROXY_ENABLED")
    os.environ["PROXY_ENABLED"] = "true"
    try:
        return hydration_v2.configured_proxy()
    except Exception:
        return None, ()
    finally:
        if old is None:
            os.environ.pop("PROXY_ENABLED", None)
        else:
            os.environ["PROXY_ENABLED"] = old


def fetch_with_fallback(document, *, timeout, proxy_url, proxy_domains):
    direct = hydration_v2.fetch_document(
        document,
        proxy_url=None,
        proxy_domains=(),
        timeout=timeout,
    )
    host_allowed = bool(
        proxy_url
        and proxy_domains
        and hydration_v2.host_matches_domains(document.get("source_url") or "", proxy_domains)
    )
    if direct["status"] in {"blocked", "error"} and host_allowed:
        proxied = hydration_v2.fetch_document(
            document,
            proxy_url=proxy_url,
            proxy_domains=proxy_domains,
            timeout=timeout,
        )
        if (
            proxied.get("status") in {"extracted", "partial"}
            or proxied.get("full_text_chars", 0) > direct.get("full_text_chars", 0)
        ):
            return proxied
        if direct["status"] == "error" and proxied["status"] == "blocked":
            return proxied
    return direct


def _clean_nul(value):
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {key: _clean_nul(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean_nul(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_clean_nul(item) for item in value)
    return value


def save_outcome(connection, row, outcome):
    existing_full = (row["full_text"] or "").strip()
    full_text = _clean_nul((outcome.get("full_text") or "")).strip()
    if len(full_text) < len(existing_full):
        full_text = existing_full

    existing_html = row["raw_html"] or ""
    raw_html = _clean_nul(outcome.get("raw_html") or existing_html)

    metadata = _clean_nul(dict(row["metadata"] or {}))
    metadata["info_hydration"] = {
        "status": outcome.get("status"),
        "final_url": outcome.get("final_url"),
        "used_proxy": bool(outcome.get("used_proxy")),
        "duration_ms": int(outcome.get("duration_ms") or 0),
        "full_text_chars": int(outcome.get("full_text_chars") or 0),
    }
    extracted_meta = _clean_nul(outcome.get("metadata") or {})
    if extracted_meta:
        metadata["article_extract"] = extracted_meta

    status = outcome.get("status") or "error"
    connection.execute(
        """UPDATE source_documents
              SET full_text=?,raw_html=?,content_status=?,fetch_error=?,
                  metadata=?::jsonb,fetched_at=now(),fetch_attempts=fetch_attempts+1,
                  updated_at=now()
            WHERE id=?""",
        (
            full_text,
            raw_html,
            status,
            _clean_nul(str(outcome.get("error") or ""))[:2000],
            json.dumps(metadata, ensure_ascii=False),
            int(row["id"]),
        ),
    )
    if full_text and full_text != existing_full:
        digest = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
        connection.execute(
            """INSERT INTO original_versions(source_document_id,sha256,source_text,origin)
               VALUES(?,?,?,'info-hydration')
               ON CONFLICT DO NOTHING""",
            (int(row["id"]), digest, full_text),
        )
    connection.commit()


def select_batch(connection, limit):
    return connection.execute(
        """SELECT d.*,pf.cluster_id,pf.route,pf.spread_score
             FROM info_prefilter_results_v1 pf
             JOIN primary_info_clusters_v1 cl
               ON cl.id=pf.cluster_id
              AND cl.canonical_document_id=pf.source_document_id
             JOIN source_documents d ON d.id=pf.source_document_id
            WHERE pf.route IN ('MUST_ANALYZE','ANALYZE')
              AND d.source_kind IN ('rss','web')
              AND length(trim(COALESCE(d.full_text,''))) < 300
              AND d.fetch_attempts < 3
              AND (
                    d.fetched_at IS NULL
                    OR d.fetched_at < now()-interval '30 minutes'
                  )
            ORDER BY
              CASE pf.route WHEN 'MUST_ANALYZE' THEN 0 ELSE 1 END,
              pf.spread_score DESC,
              pf.updated_at DESC,
              d.id DESC
            LIMIT ?""",
        (limit,),
    ).fetchall()


def select_alternate(connection, cluster_id: int, exclude_id: int):
    rows = connection.execute(
        """SELECT d.*,m.match_type
             FROM primary_info_cluster_members_v1 m
             JOIN source_documents d ON d.id=m.source_document_id
            WHERE m.cluster_id=?
              AND d.id<>?
              AND m.match_type<>'local_junk'
              AND d.source_kind IN ('rss','web')
              AND length(trim(COALESCE(d.full_text,''))) < 300
              AND d.fetch_attempts < 2
              AND length(trim(COALESCE(d.source_url,''))) > 0
            ORDER BY d.id
            LIMIT 12""",
        (cluster_id, exclude_id),
    ).fetchall()
    if not rows:
        return None
    return max(
        rows,
        key=lambda row: (
            source_priority(row),
            len((row["source_text"] or "").strip()),
            -int(row["id"]),
        ),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()

    proxy_url, proxy_domains = optional_proxy()
    connection = db.connect(readonly=False)
    stats = {"selected": 0, "extracted": 0, "partial": 0, "blocked": 0, "error": 0}
    try:
        rows = select_batch(connection, max(1, min(50, args.limit)))
        stats["selected"] = len(rows)
        for row in rows:
            outcome = fetch_with_fallback(
                dict(row),
                timeout=max(5.0, min(30.0, args.timeout)),
                proxy_url=proxy_url,
                proxy_domains=proxy_domains,
            )
            status = str(outcome.get("status") or "error")
            stats[status] = stats.get(status, 0) + 1
            save_outcome(connection, row, outcome)
            print(
                f"id={row['id']} route={row['route']} status={status} "
                f"chars={outcome.get('full_text_chars',0)} proxy={bool(outcome.get('used_proxy'))}",
                flush=True,
            )

            if status not in {"extracted", "partial"} or int(outcome.get("full_text_chars") or 0) < 300:
                alternate = select_alternate(
                    connection, int(row["cluster_id"]), int(row["id"])
                )
                if alternate:
                    alt_outcome = fetch_with_fallback(
                        dict(alternate),
                        timeout=max(5.0, min(30.0, args.timeout)),
                        proxy_url=proxy_url,
                        proxy_domains=proxy_domains,
                    )
                    alt_status = str(alt_outcome.get("status") or "error")
                    stats[alt_status] = stats.get(alt_status, 0) + 1
                    save_outcome(connection, alternate, alt_outcome)
                    print(
                        f"alternate={alternate['id']} cluster={row['cluster_id']} "
                        f"status={alt_status} chars={alt_outcome.get('full_text_chars',0)} "
                        f"proxy={bool(alt_outcome.get('used_proxy'))}",
                        flush=True,
                    )

            update_cluster_and_prefilter(connection, int(row["cluster_id"]))
            connection.commit()
        print(json.dumps(stats, ensure_ascii=False), flush=True)
    finally:
        connection.close()


if __name__ == "__main__":
    main()

