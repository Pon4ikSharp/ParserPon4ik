import hashlib
import json
import re
from typing import Any

from app import db

VERSION = "hold-v2-r2-20260930"
SCOPE_START = "2026-09-21T00:00:00"
STRONG = {
    "claim_patterns", "actor_mentions", "secondary_speaker", "conspiracy_hidden_truth",
    "official_actor_event_hold", "official_originator", "watchlist_protocol_hold",
    "weekly_relevance", "weekly_context_hold", "dynamic_terms", "dynamic_lexicon_hold",
    "framing", "claim_cooccurrence", "external_control", "delegitimization",
    "member_signal", "state_failure", "trust_erosion", "mobilization_failure",
}
MEDIUM = {
    "source_prior", "hashtags", "relay_network", "multilingual_source_profile",
    "multilingual", "telegram_profile", "telegram_profile_hold",
}
SPECIFIC = ("всу", "тцк", "мобилизац", "переговор", "перемир", "санкц", "фронт",
            "кремл", "евросоюз", "ес ", "нато")

def _prefix(value: str) -> str:
    return value.split(":", 1)[0]

def _text(row: dict[str, Any]) -> str:
    return str(row.get("full_text") or row.get("source_text") or row.get("feed_text") or "")[:6500]

def _content_signal(text: str) -> tuple[str, str] | None:
    low = re.sub(r"\s+", " ", text.lower())
    if "первопричин" in low and any(x in low for x in ("украин", "киев", "сво", "конфликт")):
        return "PROMOTE_TO_AI", "content_root_causes"
    if ("альтернатив" in low or "адг" in low) and "герман" in low and "украин" in low:
        return "REVIEW", "content_germany_support"
    return None

def classify(row: dict[str, Any]) -> tuple[str, str]:
    reasons = list(row.get("reason_codes") or [])
    prefixes = {_prefix(str(x)) for x in reasons}
    reason_text = " ".join(str(x) for x in reasons).lower()
    if int(row.get("influence_score") or 0) >= 2 or int(row.get("spread_score") or 0) >= 3 or prefixes & STRONG:
        return "PROMOTE_TO_AI", "strong_prefilter_signal"
    if any(str(x).startswith("telegram_profile:pseudo_ua") for x in reasons):
        return "PROMOTE_TO_AI", "pseudo_ua"
    content = _content_signal(_text(row))
    if content:
        return content
    if int(row.get("domain_score") or 0) >= 12 or prefixes & MEDIUM or any(term in reason_text for term in SPECIFIC):
        return "REVIEW", "contextual_hold"
    return "ARCHIVE", "low_specificity_hold"

def _fingerprint(row: dict[str, Any]) -> str:
    payload = [row.get("domain_score"), row.get("influence_score"), row.get("spread_score"),
               row.get("reason_codes") or [], _text(row)]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def _rows(connection: db.Connection, scope_start: str) -> list[dict[str, Any]]:
    query = """
    SELECT p.source_document_id,p.domain_score,p.influence_score,p.spread_score,p.reason_codes,
           d.full_text,d.source_text,d.feed_text,
           h.input_fingerprint AS existing_fingerprint,h.classifier_version AS existing_version
      FROM info_prefilter_results_v1 p
      JOIN source_documents d ON d.id=p.source_document_id
 LEFT JOIN grok_document_analysis_v1 g ON g.source_document_id=p.source_document_id
 LEFT JOIN hold_v2_results_v1 h ON h.source_document_id=p.source_document_id
     WHERE p.route='HOLD' AND g.source_document_id IS NULL
       AND (h.source_document_id IS NULL OR h.classifier_version<>? OR p.updated_at>h.updated_at
            OR (d.fetched_at IS NOT NULL AND d.fetched_at>h.updated_at))
       AND COALESCE(NULLIF(d.published_at,'')::timestamptz,NULLIF(d.created_at,'')::timestamptz)
           >= (?::timestamp AT TIME ZONE 'Europe/Kyiv')
       AND COALESCE(NULLIF(d.published_at,'')::timestamptz,NULLIF(d.created_at,'')::timestamptz)
           <= now()+interval '5 minutes'
    """
    return [dict(r) for r in connection.execute(query, (VERSION, scope_start)).fetchall()]

def sync(connection: db.Connection, scope_start: str = SCOPE_START) -> dict[str, int]:
    stats = {"seen": 0, "updated": 0, "unchanged": 0}
    for row in _rows(connection, scope_start):
        stats["seen"] += 1
        fingerprint = _fingerprint(row)
        if row.get("existing_fingerprint") == fingerprint and row.get("existing_version") == VERSION:
            connection.execute("UPDATE hold_v2_results_v1 SET updated_at=now() WHERE source_document_id=?", (int(row["source_document_id"]),))
            stats["unchanged"] += 1
            continue
        disposition, reason = classify(row)
        connection.execute("""
            INSERT INTO hold_v2_results_v1
                (source_document_id,disposition,reason,input_fingerprint,classifier_version,processed_at,updated_at)
            VALUES (?,?,?,?,?,now(),now())
            ON CONFLICT (source_document_id) DO UPDATE SET
                disposition=excluded.disposition,reason=excluded.reason,
                matched_story_id=NULL,matched_story_key='',canonical_document_id=NULL,
                token_dice=NULL,char_ratio=NULL,length_ratio=NULL,
                input_fingerprint=excluded.input_fingerprint,classifier_version=excluded.classifier_version,
                processed_at=excluded.processed_at,updated_at=now()
        """, (int(row["source_document_id"]), disposition, reason, fingerprint, VERSION))
        stats["updated"] += 1
    return stats

def current_counts(connection: db.Connection, scope_start: str = SCOPE_START) -> dict[str, int]:
    rows = connection.execute("""
        SELECT h.disposition,count(*)::int AS n
          FROM hold_v2_results_v1 h
          JOIN info_prefilter_results_v1 p ON p.source_document_id=h.source_document_id
          JOIN source_documents d ON d.id=h.source_document_id
     LEFT JOIN grok_document_analysis_v1 g ON g.source_document_id=h.source_document_id
         WHERE p.route='HOLD' AND g.source_document_id IS NULL
           AND COALESCE(NULLIF(d.published_at,'')::timestamptz,NULLIF(d.created_at,'')::timestamptz)
               >= (?::timestamp AT TIME ZONE 'Europe/Kyiv')
           AND COALESCE(NULLIF(d.published_at,'')::timestamptz,NULLIF(d.created_at,'')::timestamptz)
               <= now()+interval '5 minutes'
         GROUP BY h.disposition
    """, (scope_start,)).fetchall()
    return {str(r["disposition"]): int(r["n"]) for r in rows}

def sync_now(scope_start: str = SCOPE_START) -> dict[str, Any]:
    connection = db.connect()
    try:
        schema_path = db.ROOT / "database" / "hold_v2_v1.sql"
        connection.raw.execute(schema_path.read_text(encoding="utf-8"))
        connection.commit()
        stats = sync(connection, scope_start)
        connection.commit()
        stats["counts"] = current_counts(connection, scope_start)
        return stats
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
