from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

ROLE_SCORE = {
    "originator": 700, "official": 600, "commentary": 220,
    "aggregator": 120, "unclear": 80, "relay": 0,
}
DECISION_SCORE = {"KEEP": 500, "CONTEXT": 80, "REVIEW": -500, "DROP": -2000}
RELATION_SCORE = {"new_candidate": 260, "development_candidate": 180, "duplicate_candidate": -220}


def _source_bonus(title: str) -> int:
    s = (title or "").lower().strip()
    if "news-pravda" in s or s.startswith("pravda "):
        return -3000
    if "лента новостей" in s:
        return -1500
    if "сайт президента россии" in s or "kremlin" in s:
        return 5200
    if "мид россии" in s or "mid.ru" in s:
        return 5000
    if "минобороны россии" in s:
        return 4900
    if "россия в оон" in s or "russiaun" in s:
        return 4700
    if s == "тасс" or s.startswith("тасс "):
        return 4300
    if s == "риа новости":
        return 3700
    if "риа новости крым" in s:
        return 2500
    return 0


def _parse_ts(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _candidate_score(meta: dict[str, Any], row: dict[str, Any] | None) -> float:
    role = str((row or {}).get("source_role") or "unclear")
    score = float(ROLE_SCORE.get(role, 0) + _source_bonus(str(meta.get("source") or "")))
    text_len = int(meta.get("text_len") or len(str(meta.get("text") or "")))
    score += min(60.0, text_len / 90.0)
    score += DECISION_SCORE.get(str((row or {}).get("decision") or ""), 0)
    score += RELATION_SCORE.get(str((row or {}).get("story_relation") or ""), 0)
    route = str(meta.get("route") or "")
    if route == "MUST_ANALYZE": score += 25
    elif route == "ANALYZE": score += 12
    return score


def _story_text(row: dict[str, Any]) -> tuple[str, str]:
    title = str(row.get("information_trigger") or row.get("main_claim") or "").strip()[:500]
    parts = [str(row.get("main_claim") or "").strip(), str(row.get("framing") or "").strip()]
    summary = " ".join(x for x in parts if x).strip()[:4000]
    return title, summary


def _external_candidate(cur, doc_id: int) -> tuple[dict[str, Any], dict[str, Any]] | None:
    row = cur.execute(
        '''SELECT d.id,d.source_title,d.published_at,d.created_at,
                  length(COALESCE(NULLIF(d.full_text,''),NULLIF(d.source_text,''),NULLIF(d.feed_text,''),'')) AS text_len,
                  g.source_role,g.information_trigger,g.main_claim,g.framing,g.target_audiences
             FROM source_documents d
        LEFT JOIN grok_document_analysis_v1 g ON g.source_document_id=d.id
            WHERE d.id=%s''', (int(doc_id),)
    ).fetchone()
    if not row:
        return None
    r = dict(row)
    meta = {
        "source_document_id": int(r["id"]), "source": r.get("source_title") or "",
        "published_at": r.get("published_at") or r.get("created_at"),
        "route": "", "text": "", "text_len": int(r.get("text_len") or 0),
    }
    analysis = {
        "source_role": r.get("source_role") or "unclear",
        "decision": r.get("decision") or "", "story_relation": "existing_canonical",
        "information_trigger": r.get("information_trigger") or "",
        "main_claim": r.get("main_claim") or "", "framing": r.get("framing") or "",
        "target_audiences": r.get("target_audiences") or [],
    }
    return meta, analysis


def sync_story_groups(cur, batch: dict[str, Any], result: dict[str, Any]) -> dict[str, int]:
    item_map = {int(x["source_document_id"]): x for x in batch.get("items", [])}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in result.get("results", []):
        key = str(row.get("matched_story_key") or "").strip()[:180]
        if not key or row.get("story_relation") in {"none", "unclear"}:
            continue
        if not row.get("keep_for_analysis") or row.get("insufficient_text"):
            continue
        if int(row.get("confidence") or 0) < 60:
            continue
        groups[key].append(row)

    counts = Counter()
    for key, rows in groups.items():
        existing = cur.execute(
            "SELECT * FROM grok_stories_v1 WHERE story_key=%s FOR UPDATE", (key,)
        ).fetchone()
        candidates: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
        for row in rows:
            doc_id = int(row["source_document_id"])
            meta = dict(item_map.get(doc_id) or {})
            meta.setdefault("source_document_id", doc_id)
            candidates[doc_id] = (meta, row)
            other = row.get("duplicate_of_document_id")
            if other is not None and int(other) not in candidates:
                ext = _external_candidate(cur, int(other))
                if ext: candidates[int(other)] = ext
        if existing and existing.get("canonical_document_id") is not None:
            old_id = int(existing["canonical_document_id"])
            if old_id not in candidates:
                ext = _external_candidate(cur, old_id)
                if ext: candidates[old_id] = ext
        if not candidates:
            counts["groups_skipped"] += 1
            continue

        # Canonical stability guard: a fresh duplicate must never replace an existing
        # representative merely because it comes from a preferred outlet. For an
        # existing Story, only the current canonical or a strong KEEP new/development
        # item may compete for canonical. For a new Story prefer KEEP new/development
        # candidates; fall back to all candidates only when no such item exists.
        old_canonical_id = int(existing.get("canonical_document_id") or 0) if existing else 0
        if existing and old_canonical_id:
            eligible = {
                did: pair for did, pair in candidates.items()
                if did == old_canonical_id or (
                    str(pair[1].get("decision") or "") == "KEEP"
                    and str(pair[1].get("story_relation") or "") in {"new_candidate", "development_candidate"}
                    and not bool(pair[1].get("insufficient_text"))
                )
            }
        else:
            eligible = {
                did: pair for did, pair in candidates.items()
                if str(pair[1].get("decision") or "") == "KEEP"
                and str(pair[1].get("story_relation") or "") in {"new_candidate", "development_candidate"}
                and not bool(pair[1].get("insufficient_text"))
            }
        pool = eligible or candidates
        canonical_id, (canonical_meta, canonical_row) = max(
            pool.items(), key=lambda kv: _candidate_score(kv[1][0], kv[1][1])
        )
        title, summary = _story_text(canonical_row)
        audiences = []
        timestamps = []
        for row in rows:
            for audience in row.get("target_audiences") or []:
                if audience not in audiences: audiences.append(audience)
            ts = _parse_ts((item_map.get(int(row["source_document_id"])) or {}).get("published_at"))
            if ts: timestamps.append(ts)
        first_seen = min(timestamps) if timestamps else None
        last_seen = max(timestamps) if timestamps else None

        if existing:
            old_first = existing.get("first_seen_at"); old_last = existing.get("last_seen_at")
            if old_first is not None: first_seen = min(x for x in [first_seen, old_first] if x is not None)
            if old_last is not None: last_seen = max(x for x in [last_seen, old_last] if x is not None)
            if not title: title = str(existing.get("title") or "")
            if not summary: summary = str(existing.get("summary") or "")
            old_aud = list(existing.get("target_audiences") or [])
            for a in old_aud:
                if a not in audiences: audiences.append(a)
        if existing:
            cur.execute(
                '''UPDATE grok_stories_v1 SET canonical_document_id=%s,title=%s,summary=%s,
                          target_audiences=%s::jsonb,first_seen_at=%s,last_seen_at=%s,updated_at=now()
                     WHERE id=%s''',
                (canonical_id,title,summary,json.dumps(audiences,ensure_ascii=False),first_seen,last_seen,int(existing['id']))
            )
            story_id = int(existing['id']); counts['stories_updated'] += 1
        else:
            story = cur.execute(
                '''INSERT INTO grok_stories_v1(
                     story_key,canonical_document_id,title,summary,status,narrative_keys,
                     target_audiences,likely_objectives,first_seen_at,last_seen_at)
                   VALUES(%s,%s,%s,%s,'active','[]'::jsonb,%s::jsonb,'[]'::jsonb,%s,%s)
                   RETURNING id''',
                (key,canonical_id,title,summary,json.dumps(audiences,ensure_ascii=False),first_seen,last_seen)
            ).fetchone()
            story_id = int(story['id']); counts['stories_created'] += 1

        cur.execute(
            "UPDATE grok_story_members_v1 SET relation='duplicate' WHERE story_id=%s AND relation='canonical' AND source_document_id<>%s",
            (story_id,canonical_id)
        )
        canonical_added = False
        for row in rows:
            doc_id = int(row['source_document_id'])
            relation = 'canonical' if doc_id == canonical_id else (
                'development' if row.get('story_relation') == 'development_candidate' else 'duplicate'
            )
            cur.execute(
                '''INSERT INTO grok_story_members_v1(story_id,source_document_id,relation,added_value)
                   VALUES(%s,%s,%s,%s)
                   ON CONFLICT(source_document_id) DO UPDATE SET
                     story_id=excluded.story_id,relation=excluded.relation,
                     added_value=excluded.added_value,attached_at=now()''',
                (story_id,doc_id,relation,str(row.get('added_value') or '')[:4000])
            )
            novelty = 'new' if relation == 'canonical' else ('development' if relation == 'development' else 'duplicate')
            dup_id = canonical_id if relation == 'duplicate' else None
            cur.execute(
                '''UPDATE grok_document_analysis_v1
                      SET story_key=%s,novelty=%s,duplicate_of_document_id=%s,updated_at=now()
                    WHERE source_document_id=%s''',
                (key,novelty,dup_id,doc_id)
            )
            counts[f'members_{relation}'] += 1
            canonical_added = canonical_added or relation == 'canonical'
        if not canonical_added:
            cur.execute(
                '''INSERT INTO grok_story_members_v1(story_id,source_document_id,relation,added_value)
                   VALUES(%s,%s,'canonical','')
                   ON CONFLICT(source_document_id) DO UPDATE SET
                     story_id=excluded.story_id,relation='canonical',attached_at=now()''',
                (story_id,canonical_id)
            )
            counts['members_canonical_external'] += 1
        if existing and int(existing.get('canonical_document_id') or 0) != canonical_id:
            counts['canonical_upgraded'] += 1
    counts['groups'] = len(groups)
    return dict(counts)


def detach_story_memberships(cur, doc_ids: list[int]) -> list[int]:
    if not doc_ids:
        return []
    rows = cur.execute(
        "SELECT DISTINCT story_id FROM grok_story_members_v1 WHERE source_document_id = ANY(%s)",
        (doc_ids,),
    ).fetchall()
    story_ids = [int(r["story_id"]) for r in rows]
    cur.execute("DELETE FROM grok_story_members_v1 WHERE source_document_id = ANY(%s)", (doc_ids,))
    return story_ids


def reconcile_touched_stories(cur, story_ids: list[int]) -> dict[str, int]:
    counts = Counter()
    for story_id in sorted(set(int(x) for x in story_ids)):
        story = cur.execute("SELECT * FROM grok_stories_v1 WHERE id=%s FOR UPDATE", (story_id,)).fetchone()
        if not story:
            continue
        members = cur.execute(
            "SELECT source_document_id FROM grok_story_members_v1 WHERE story_id=%s", (story_id,)
        ).fetchall()
        member_ids = [int(r["source_document_id"]) for r in members]
        if not member_ids:
            cur.execute("DELETE FROM grok_stories_v1 WHERE id=%s", (story_id,))
            counts["stories_deleted_empty"] += 1
            continue
        current = int(story.get("canonical_document_id") or 0)
        if current in member_ids:
            continue
        ranked = []
        for doc_id in member_ids:
            ext = _external_candidate(cur, doc_id)
            if ext:
                ranked.append((_candidate_score(ext[0], ext[1]), doc_id, ext[1]))
        if not ranked:
            counts["stories_unresolved"] += 1
            continue
        ranked.sort(reverse=True, key=lambda x: x[0])
        _, canonical_id, analysis = ranked[0]
        title, summary = _story_text(analysis)
        cur.execute(
            "UPDATE grok_stories_v1 SET canonical_document_id=%s,title=%s,summary=%s,updated_at=now() WHERE id=%s",
            (canonical_id, title or story.get("title") or "", summary or story.get("summary") or "", story_id),
        )
        cur.execute(
            "UPDATE grok_story_members_v1 SET relation=CASE WHEN source_document_id=%s THEN 'canonical' ELSE 'duplicate' END WHERE story_id=%s",
            (canonical_id, story_id),
        )
        counts["canonical_repaired"] += 1
    return dict(counts)
