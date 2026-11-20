from __future__ import annotations

import argparse
import bisect
import re
from collections import defaultdict
from datetime import timedelta
from difflib import SequenceMatcher
from urllib.parse import urlparse

from app import db, primary_dedup

MATCH_TYPE = "deterministic_dzen_v2"
SOURCE_STOP = {
    "news", "online", "ru", "новости", "новость", "газета",
    "телеканал", "информационный", "портал", "на", "русском",
}


def norm(value: str, source: bool = False) -> str:
    value = (value or "").lower().replace("ё", "е").replace("\xa0", " ")
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    tokens = [token for token in value.split() if token]
    if source:
        tokens = [token for token in tokens if token not in SOURCE_STOP]
    return " ".join(tokens)


def title_score(left: str, right: str) -> float:
    left, right = norm(left), norm(right)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    left_set, right_set = set(left.split()), set(right.split())
    overlap = len(left_set & right_set)
    union = len(left_set | right_set) or 1
    jaccard = overlap / union
    sequence = SequenceMatcher(None, left, right).ratio()
    containment = overlap / min(len(left_set), len(right_set))
    return max(sequence, 0.60 * containment + 0.40 * jaccard)


def source_score(left: str, right: str) -> float:
    left, right = norm(left, True), norm(right, True)
    if not left or not right:
        return 0.0
    if left == right or left in right or right in left:
        return 1.0
    left_set, right_set = set(left.split()), set(right.split())
    if not left_set or not right_set:
        return 0.0
    return max(
        SequenceMatcher(None, left, right).ratio(),
        len(left_set & right_set) / min(len(left_set), len(right_set)),
    )


def _bulk_story_map(connection, document_ids: list[int]) -> dict[int, int]:
    result: dict[int, int] = {}
    for offset in range(0, len(document_ids), 1000):
        chunk = document_ids[offset:offset + 1000]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            f"SELECT source_document_id,min(story_id) story_id "
            f"FROM grok_story_members_v1 WHERE source_document_id IN ({placeholders}) "
            "GROUP BY source_document_id",
            tuple(chunk),
        ).fetchall()
        result.update({int(row["source_document_id"]): int(row["story_id"]) for row in rows})
    return result


def build_matches(*, commit: bool = False, lookback_hours: int = 48) -> dict:
    connection = db.connect()
    try:
        dzen_rows = [dict(row) for row in connection.execute(
            """SELECT cluster_id,source_doc_id,source_name,title,article_url,published_at
                 FROM dzen_story_sources_v1
                WHERE published_at IS NOT NULL
                  AND last_seen_at >= now()-(? || ' hours')::interval
                ORDER BY published_at""",
            (max(1, int(lookback_hours)),),
        ).fetchall()]
        if not dzen_rows:
            return {"scanned": 0, "local_docs": 0, "matches": 0,
                    "unmatched": 0, "exact_url": 0, "title_time": 0}

        first_pub = min(row["published_at"] for row in dzen_rows)
        last_pub = max(row["published_at"] for row in dzen_rows)
        docs = [dict(row) for row in connection.execute(
            """SELECT d.id,d.source_title,d.source_url,
                      split_part(coalesce(nullif(d.feed_text,''),nullif(d.source_text,''),
                                          nullif(d.full_text,'')), E'\n', 1) title_line,
                      coalesce(nullif(d.published_at,'')::timestamptz,
                               nullif(d.created_at,'')::timestamptz) pub_ts
                 FROM source_documents d
                WHERE d.source_kind<>'telegram'
                  AND coalesce(nullif(d.published_at,'')::timestamptz,
                               nullif(d.created_at,'')::timestamptz)
                      BETWEEN ?::timestamptz-interval '12 hours'
                          AND ?::timestamptz+interval '12 hours'""",
            (first_pub, last_pub),
        ).fetchall()]
        by_source: dict[str, list[dict]] = defaultdict(list)
        exact_title_index: dict[tuple[str, str], list[dict]] = defaultdict(list)
        url_index: dict[str, dict] = {}
        for doc in docs:
            doc["source_norm"] = norm(doc["source_title"], True)
            doc["title_norm"] = norm(doc["title_line"])
            doc["title_tokens"] = set(doc["title_norm"].split())
            if doc["pub_ts"] and doc["source_norm"]:
                by_source[doc["source_norm"]].append(doc)
                if doc["title_norm"]:
                    exact_title_index[(doc["source_norm"], doc["title_norm"])].append(doc)
            canonical_url = primary_dedup.canonicalize_url(doc["source_url"] or "")
            if canonical_url:
                url_index.setdefault(canonical_url, doc)
        for rows in by_source.values():
            rows.sort(key=lambda row: row["pub_ts"])
        source_times = {key: [row["pub_ts"] for row in rows] for key, rows in by_source.items()}
        source_keys = list(by_source)
        compatibility_cache: dict[str, list[tuple[str, float]]] = {}
        matches = []
        unmatched = exact_url = exact_title = fuzzy_title = 0

        for dzen in dzen_rows:
            external_url = primary_dedup.canonicalize_url(dzen["article_url"] or "")
            best = None
            basis = ""
            if external_url and "dzen.ru/" not in external_url and external_url in url_index:
                doc = url_index[external_url]
                best = (1.0, 1.0, 1.0, 0.0, doc)
                basis = "exact_url"
            else:
                source_key = norm(dzen["source_name"], True)
                title_norm = norm(dzen["title"])
                title_tokens = set(title_norm.split())
                if source_key not in compatibility_cache:
                    compatibility_cache[source_key] = [
                        (key, score) for key in source_keys
                        if (score := source_score(source_key, key)) >= 0.72
                    ]
                compatible = compatibility_cache.get(source_key, [])
                exact_candidates = []
                for key, source_similarity in compatible:
                    for doc in exact_title_index.get((key, title_norm), []):
                        delta = abs((doc["pub_ts"]-dzen["published_at"]).total_seconds())/3600
                        if delta <= 12:
                            exact_candidates.append((delta, source_similarity, doc))
                if exact_candidates:
                    delta, source_similarity, doc = min(exact_candidates, key=lambda item: item[0])
                    best = (1.0, 1.0, source_similarity, delta, doc)
                    basis = "exact_title"
                else:
                    candidates = []
                    for key, source_similarity in compatible:
                        rows = by_source[key]
                        times = source_times[key]
                        lo = bisect.bisect_left(times, dzen["published_at"] - timedelta(hours=12))
                        hi = bisect.bisect_right(times, dzen["published_at"] + timedelta(hours=12))
                        for doc in rows[lo:hi]:
                            if not title_tokens or not doc["title_tokens"]:
                                continue
                            overlap = len(title_tokens & doc["title_tokens"]) / min(len(title_tokens), len(doc["title_tokens"]))
                            if overlap < 0.72:
                                continue
                            if not (0.65 <= len(doc["title_norm"]) / max(1, len(title_norm)) <= 1.55):
                                continue
                            candidates.append((doc, source_similarity))
                    for doc, source_similarity in candidates:
                        title_similarity = title_score(dzen["title"], doc["title_line"])
                        if title_similarity < 0.90:
                            continue
                        delta = abs((doc["pub_ts"]-dzen["published_at"]).total_seconds())/3600
                        time_score = max(0.0, 1-delta/12)
                        confidence = 0.74*title_similarity + 0.16*source_similarity + 0.10*time_score
                        if best is None or confidence > best[0]:
                            best = (confidence, title_similarity, source_similarity, delta, doc)
                            basis = "fuzzy_title"

            if best is None or best[0] < 0.95:
                unmatched += 1
                continue
            confidence,title_similarity,source_similarity,delta_hours,doc = best
            if basis == "exact_url": exact_url += 1
            elif basis == "exact_title": exact_title += 1
            else: fuzzy_title += 1
            matches.append({
                "cluster_id": dzen["cluster_id"], "source_doc_id": dzen["source_doc_id"],
                "doc_id": int(doc["id"]), "confidence": float(confidence),
                "title_score": float(title_similarity), "source_score": float(source_similarity),
                "delta_hours": float(delta_hours), "basis": basis,
            })

        story_map = _bulk_story_map(connection, sorted({m["doc_id"] for m in matches}))
        if commit:
            for match in matches:
                story_id = story_map.get(match["doc_id"])
                notes = (
                    f"basis={match['basis']}; dzen_doc={match['source_doc_id']}; "
                    f"title={match['title_score']:.3f}; source={match['source_score']:.3f}; "
                    f"delta_h={match['delta_hours']:.2f}"
                )
                connection.execute(
                    """INSERT INTO dzen_story_matches_v1(
                           cluster_id,grok_story_id,source_document_id,match_type,
                           confidence,matched_at,notes)
                       VALUES(?,?,?,?,?,now(),?)
                       ON CONFLICT(cluster_id,match_type,source_document_id) DO UPDATE SET
                         grok_story_id=excluded.grok_story_id,
                         confidence=excluded.confidence,
                         matched_at=now(),notes=excluded.notes""",
                    (match["cluster_id"], story_id, match["doc_id"], MATCH_TYPE,
                     round(match["confidence"], 4), notes),
                )
            connection.commit()

        return {
            "scanned": len(dzen_rows), "local_docs": len(docs),
            "matches": len(matches), "unmatched": unmatched,
            "exact_url": exact_url, "exact_title": exact_title, "fuzzy_title": fuzzy_title,
            "with_grok_story": sum(1 for m in matches if m["doc_id"] in story_map),
            "committed": bool(commit), "lookback_hours": int(lookback_hours),
        }
    finally:
        connection.close()



def refresh_source_candidates() -> dict:
    connection = db.connect()
    try:
        aggregates = [dict(row) for row in connection.execute("""
            SELECT zs.source_name,
                   count(*) FILTER (WHERE zs.last_seen_at>=now()-interval '24 hours')::int publications_24h,
                   count(DISTINCT zs.cluster_id) FILTER (WHERE zs.last_seen_at>=now()-interval '24 hours')::int stories_24h,
                   count(DISTINCT zs.cluster_id) FILTER (
                       WHERE zs.last_seen_at>=now()-interval '24 hours' AND st.discovery_score>=4
                   )::int relevant_stories_24h,
                   count(*)::int publications_7d,
                   count(DISTINCT zs.cluster_id)::int stories_7d,
                   count(DISTINCT zs.cluster_id) FILTER (WHERE st.discovery_score>=4)::int relevant_stories_7d,
                   min(zs.first_seen_at) first_seen_at,max(zs.last_seen_at) last_seen_at,
                   (array_agg(zs.article_url ORDER BY zs.last_seen_at DESC) FILTER (WHERE zs.article_url<>''))[1] sample_article_url
              FROM dzen_story_sources_v1 zs
              JOIN dzen_stories_v1 st ON st.cluster_id=zs.cluster_id
             WHERE zs.last_seen_at>=now()-interval '7 days' AND zs.source_name<>''
             GROUP BY zs.source_name
        """).fetchall()]
        registry = [dict(row) for row in connection.execute(
            "SELECT id,title,enabled,for_info FROM sources ORDER BY id"
        ).fetchall()]
        registered = 0
        for item in aggregates:
            best = (0.0, None)
            for source in registry:
                score = source_score(item["source_name"], source["title"])
                if score > best[0]:
                    best = (score, source)
            score, source = best
            matched_id = int(source["id"]) if source is not None and score>=0.72 else None
            matched_title = source["title"] if matched_id is not None else ""
            if matched_id is not None:
                registered += 1
            connection.execute("""
                INSERT INTO dzen_source_candidates_v1(
                    source_name,publications_24h,stories_24h,relevant_stories_24h,
                    publications_7d,stories_7d,relevant_stories_7d,
                    matched_source_id,matched_source_title,source_match_confidence,
                    first_seen_at,last_seen_at,sample_article_url,sample_domain,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,now())
                ON CONFLICT(source_name) DO UPDATE SET
                    publications_24h=excluded.publications_24h,stories_24h=excluded.stories_24h,
                    relevant_stories_24h=excluded.relevant_stories_24h,
                    publications_7d=excluded.publications_7d,stories_7d=excluded.stories_7d,
                    relevant_stories_7d=excluded.relevant_stories_7d,
                    matched_source_id=excluded.matched_source_id,
                    matched_source_title=excluded.matched_source_title,
                    source_match_confidence=excluded.source_match_confidence,
                    first_seen_at=excluded.first_seen_at,last_seen_at=excluded.last_seen_at,
                    sample_article_url=excluded.sample_article_url,sample_domain=excluded.sample_domain,
                    updated_at=now()
            """,(
                item["source_name"],item["publications_24h"],item["stories_24h"],
                item["relevant_stories_24h"],item["publications_7d"],item["stories_7d"],
                item["relevant_stories_7d"],matched_id,matched_title,round(score,5),
                item["first_seen_at"],item["last_seen_at"],
                item.get("sample_article_url") or "",
                (urlparse(item.get("sample_article_url") or "").hostname or "").lower().removeprefix("www."),
            ))
        connection.execute("DELETE FROM dzen_source_candidates_v1 WHERE last_seen_at<now()-interval '7 days'")
        connection.commit()
        return {"candidate_sources": len(aggregates), "registered_sources": registered,
                "unregistered_sources": len(aggregates)-registered}
    finally:
        connection.close()

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--lookback-hours", type=int, default=48)
    args = parser.parse_args()
    import json
    print(json.dumps(build_matches(commit=args.commit, lookback_hours=args.lookback_hours),
                     ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
