import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db, info_prefilter, primary_dedup

CUTOFF = os.getenv("INFO_PREFILTER_CUTOFF", "2026-09-13 21:01:00+00")
VERSION = info_prefilter.RULES_VERSION
LIVE_BATCH_SIZE = max(50, min(1500, int(os.getenv("INFO_PREFILTER_LIVE_BATCH", "1000"))))
BACKLOG_BATCH_SIZE = max(0, min(1500, int(os.getenv("INFO_PREFILTER_BACKLOG_BATCH", "250"))))
LIVE_LOOKBACK_HOURS = max(1, min(72, int(os.getenv("INFO_PREFILTER_LIVE_LOOKBACK_HOURS", "12"))))
BATCH_SIZE = LIVE_BATCH_SIZE + BACKLOG_BATCH_SIZE
RUN_LOG = ROOT / "runtime" / "logs" / "primary_info_queue.log"


def write_run_log(payload):
    try:
        RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
        with RUN_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def ts(row):
    return row["published_at"] or row["created_at"]


def json_value(value):
    return json.dumps(value, ensure_ascii=False)


def exact_window_hours(text: str) -> int:
    """Time window for exact-copy clustering.

    Short service headlines/alerts recur naturally and must not become one
    week-long propagation cluster. Long identical articles can legitimately
    be syndicated for longer.
    """
    size = len(primary_dedup.normalize_text(text or ""))
    if size <= 80:
        return 4
    if size <= 160:
        return 8
    if size <= 350:
        return 24
    return 72


DYNAMIC_LEXICON_CACHE = {}
DYNAMIC_CHANNEL_HASHTAG_ALLOWLIST = {
    "легитимный": {"#раскладка", "#слухи", "#экономагеддон"},
}

DYNAMIC_NARRATIVE_HASHTAG_STEMS = (
    "режим", "преступлен", "злочин", "крах", "коллапс", "колапс",
    "банкрот", "дефолт", "катастроф", "могилиз", "бусиф", "капитуляц",
    "капітуляц", "репресс", "хунт", "диктат", "глобалист", "марионет",
    "экономагеддон", "економагеддон", "нацист", "фашист",
)
DYNAMIC_GENERIC_HASHTAG_STEMS = (
    "выбор", "вибор", "госдума", "дума202", "парламент", "прямойэфир",
    "дайджест", "россия", "украина", "україна", "сша", "евросоюз",
    "євросоюз", "нато", "брикс", "снг", "африка", "победа", "перемога",
    "индия", "пакистан", "иран", "ирак", "сирия", "израиль", "турция",
    "франция", "германия", "казахстан", "узбекистан", "кыргыз", "грузия",
    "чечня", "карабах", "хусит", "ксир", "спецслужб",
)


def dynamic_lexicon_term_allowed(row):
    if bool(row["promoted"]):
        return True
    term_type = str(row["term_type"] or "")
    if term_type not in {"hashtag", "label"}:
        return False
    term = info_prefilter._norm(str(row["term"] or ""))
    if any(stem in term for stem in DYNAMIC_NARRATIVE_HASHTAG_STEMS):
        return True
    count7 = max(1, int(row["count_7d"] or 0))
    target = int(row["target_docs_7d"] or 0)
    channels = int(row["global_channels_7d"] or 0)
    ratio = target / count7
    if term_type == "label":
        return 2 <= count7 <= 40 and ratio >= 0.75 and channels <= 2 and int(row["candidate_score"] or 0) >= 7
    if not (2 <= count7 <= 30 and ratio >= 0.85 and channels <= 1):
        return False
    if any(stem in term for stem in DYNAMIC_GENERIC_HASHTAG_STEMS):
        return False
    return True


ROUTE_RANK = {
    "DROP_HARD": 0,
    "DROP_SOFT": 1,
    "HOLD": 2,
    "ANALYZE": 3,
    "MUST_ANALYZE": 4,
}


def dynamic_terms_for_member(connection, row):
    if str(row["source_kind"] or "").lower() != "telegram":
        return ()
    channel_key = str(row["source_id"] or row["source_title"] or "unknown")
    terms = DYNAMIC_LEXICON_CACHE.get(channel_key)
    if terms is None:
        rows = connection.execute(
            """SELECT term,term_type,count_7d,target_docs_7d,
                      global_channels_7d,candidate_score,promoted
                 FROM telegram_lexicon_terms_v1
                WHERE channel_key=?
                  AND (promoted OR candidate_score>=8)
                ORDER BY promoted DESC,candidate_score DESC,count_7d DESC
                LIMIT 240""",
            (channel_key,),
        ).fetchall()
        channel_name = info_prefilter._norm(str(row["source_title"] or ""))
        forced = DYNAMIC_CHANNEL_HASHTAG_ALLOWLIST.get(channel_name, set())
        terms = tuple(
            str(item["term"])
            for item in rows
            if dynamic_lexicon_term_allowed(item)
            or info_prefilter._norm(str(item["term"] or "")) in forced
        )[:120]
        DYNAMIC_LEXICON_CACHE[channel_key] = terms
    if not terms:
        return ()
    body = info_prefilter._norm(
        (row["full_text"] or row["source_text"] or "")
    )
    return tuple(term for term in terms if info_prefilter._norm(term) in body)[:12]


def merge_prefilter_results(base, extra, document_id):
    rank_base = ROUTE_RANK.get(base.route, 0)
    rank_extra = ROUTE_RANK.get(extra.route, 0)
    route = extra.route if rank_extra > rank_base else base.route
    reason_codes = tuple(dict.fromkeys((
        *base.reason_codes,
        f"member_signal:{document_id}:{extra.route}",
        *extra.reason_codes[:6],
    )))
    return info_prefilter.PrefilterResult(
        route=route,
        hard_filter_reason=base.hard_filter_reason if route == "DROP_HARD" else "",
        domain_score=max(base.domain_score, extra.domain_score),
        influence_score=max(base.influence_score, extra.influence_score),
        spread_score=base.spread_score,
        actor_hits=tuple(dict.fromkeys((*base.actor_hits, *extra.actor_hits))),
        narrative_hits=tuple(dict.fromkeys((*base.narrative_hits, *extra.narrative_hits))),
        hashtag_hits=tuple(dict.fromkeys((*base.hashtag_hits, *extra.hashtag_hits))),
        reason_codes=reason_codes,
    )


def ensure_cluster(connection, row):
    connection.execute(
        """INSERT INTO primary_info_clusters_v1
           (canonical_document_id,classifier_version,first_seen_at,last_seen_at,
            source_groups,audiences,country_spaces,representative_sources)
           VALUES(?,?,NULLIF(?,'')::timestamptz,NULLIF(?,'')::timestamptz,
                  ?::jsonb,?::jsonb,?::jsonb,?::jsonb)
           ON CONFLICT(canonical_document_id) DO NOTHING""",
        (
            int(row["id"]), VERSION, ts(row), ts(row),
            json_value(row["groups"] or []),
            json_value(row["audiences"] or []),
            json_value(row["country_spaces"] or []),
            json_value([{
                "source_id": row["source_id"],
                "title": row["source_title"],
                "url": row["source_url"],
            }]),
        ),
    )
    return int(connection.execute(
        "SELECT id FROM primary_info_clusters_v1 WHERE canonical_document_id=?",
        (int(row["id"]),),
    ).fetchone()["id"])



PRIMARY_SOURCE_DOMAINS = {
    "kremlin.ru": 140,
    "mid.ru": 135,
    "government.ru": 130,
    "scrf.gov.ru": 130,
    "council.gov.ru": 125,
    "duma.gov.ru": 125,
    "un.org": 120,
    "tass.ru": 110,
    "ria.ru": 80,
}
PRIMARY_SOURCE_TITLES = {
    "мид россии": 135,
    "президент россии": 140,
    "кремль": 140,
    "совет безопасности": 130,
    "совет федерации": 125,
    "государственная дума": 125,
    "тасс": 110,
    "риа новости": 80,
}


def source_domain(url):
    try:
        host = (urlparse(str(url or "")).hostname or "").lower().strip(".")
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def is_relay_network(row):
    host = source_domain(row["source_url"])
    title = str(row["source_title"] or "").lower()
    return (
        host == "news-pravda.com"
        or host.endswith(".news-pravda.com")
        or title.startswith("pravda ")
    )


def source_priority(row):
    host = source_domain(row["source_url"])
    priority = 0
    for domain, value in PRIMARY_SOURCE_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            priority = max(priority, value)
    title = str(row["source_title"] or "").lower()
    for needle, value in PRIMARY_SOURCE_TITLES.items():
        if needle in title:
            priority = max(priority, value)
    return priority


def choose_canonical(connection, cluster_id, current_id, members):
    # Preserve an already analyzed member so completed analysis never becomes
    # detached from its evidence.
    analyzed = connection.execute(
        """SELECT r.source_document_id
             FROM primary_info_results_v1 r
             JOIN primary_info_cluster_members_v1 m
               ON m.source_document_id=r.source_document_id
            WHERE m.cluster_id=?
            ORDER BY (r.source_document_id=?) DESC,r.updated_at DESC
            LIMIT 1""",
        (cluster_id, current_id),
    ).fetchone()
    if analyzed:
        return int(analyzed["source_document_id"])

    usable = [row for row in members if row["match_type"] != "local_junk"]
    if not usable:
        return int(current_id)

    # news-pravda.com is a multilingual relay/distribution network. When a
    # cluster also contains a non-relay source, never present the relay copy as
    # the origin/reference document. If the cluster contains only relay nodes,
    # keep the normal earliest-member fallback.
    non_relay = [row for row in usable if not is_relay_network(row)]
    canonical_pool = non_relay or usable

    # Canonical means origin/reference source, not "the page from which we
    # managed to scrape the most characters". Known primary/official sources
    # therefore outrank accessible copies even when the origin blocks fetches.
    prioritized = [row for row in canonical_pool if source_priority(row) > 0]
    if prioritized:
        selected = max(
            prioritized,
            key=lambda row: (
                source_priority(row),
                -int(row["id"]),
            ),
        )
    else:
        # With no source-priority evidence, keep the earliest cluster member as
        # the conservative origin candidate. Fullness is handled separately by
        # analysis_document_id.
        selected = canonical_pool[0]

    selected_id = int(selected["id"])
    conflict = connection.execute(
        "SELECT id FROM primary_info_clusters_v1 WHERE canonical_document_id=? AND id<>?",
        (selected_id, cluster_id),
    ).fetchone()
    return int(current_id) if conflict else selected_id


def choose_analysis_document(canonical_id, members):
    canonical = next(
        (row for row in members if int(row["id"]) == int(canonical_id)),
        members[0],
    )
    canonical_full = (canonical["full_text"] or "").strip()
    if len(canonical_full) >= 300:
        return canonical

    usable = [row for row in members if row["match_type"] != "local_junk"]
    if not usable:
        return canonical

    status_rank = {
        "manual": 5,
        "extracted": 5,
        "telegram": 4,
        "partial": 2,
        "pending": 1,
        "blocked": 0,
        "error": 0,
    }

    def score(row):
        full_len = len((row["full_text"] or "").strip())
        text_len = len((row["full_text"] or row["source_text"] or "").strip())
        return (
            1 if full_len >= 300 else 0,
            status_rank.get(str(row["content_status"] or "").lower(), 0),
            min(full_len, 50000),
            min(text_len, 50000),
            source_priority(row),
            -int(row["id"]),
        )

    best = max(usable, key=score)
    # If nobody has a meaningfully better text, keep the origin as analysis text too.
    if len((best["full_text"] or "").strip()) < 300:
        return canonical
    return best


def reseat_canonical(connection, cluster_id, old_id, new_id, members):
    if int(old_id) == int(new_id):
        return
    old_row = next((row for row in members if int(row["id"]) == int(old_id)), None)
    new_row = next((row for row in members if int(row["id"]) == int(new_id)), None)
    if not old_row or not new_row:
        return

    same_hash = bool(old_row["content_hash"] and old_row["content_hash"] == new_row["content_hash"])
    if same_hash:
        old_match, old_similarity = "exact", 1.0
    else:
        old_text = (old_row["full_text"] or old_row["source_text"] or "").strip()
        new_text = (new_row["full_text"] or new_row["source_text"] or "").strip()
        old_match = "near"
        old_similarity = float(primary_dedup.near_similarity(old_text, new_text)) if old_text and new_text else 0.0

    connection.execute(
        "UPDATE primary_info_clusters_v1 SET canonical_document_id=?,updated_at=now() WHERE id=?",
        (new_id, cluster_id),
    )
    connection.execute(
        """UPDATE primary_info_cluster_members_v1
              SET match_type=?,similarity=?,propagation_role='repeater'
            WHERE cluster_id=? AND source_document_id=?""",
        (old_match, old_similarity, cluster_id, old_id),
    )
    connection.execute(
        """UPDATE primary_info_cluster_members_v1
              SET match_type='canonical',similarity=1.0,propagation_role='origin'
            WHERE cluster_id=? AND source_document_id=?""",
        (cluster_id, new_id),
    )
    connection.execute(
        """UPDATE primary_info_cluster_members_v1
              SET propagation_role='repeater'
            WHERE cluster_id=? AND source_document_id<>? AND propagation_role='origin'""",
        (cluster_id, new_id),
    )
    connection.execute(
        """UPDATE primary_info_queue_v1 q
              SET status='clustered',decision='INHERIT',canonical_document_id=?,
                  batch_id='',locked_at=NULL,retry_at=NULL,
                  reason=?,completed_at=now(),updated_at=now()
            WHERE q.source_document_id=?
              AND NOT EXISTS(
                  SELECT 1 FROM primary_info_results_v1 r
                   WHERE r.source_document_id=q.source_document_id
              )""",
        (new_id, f"canonical reselected to {new_id}", old_id),
    )


def update_cluster_and_prefilter(connection, cluster_id):
    cluster = connection.execute(
        "SELECT * FROM primary_info_clusters_v1 WHERE id=?",
        (cluster_id,),
    ).fetchone()
    if not cluster:
        return

    members = connection.execute(
        """SELECT d.id,d.source_id,d.source_kind,d.source_title,d.source_url,
                  d.source_text,d.feed_text,d.full_text,d.published_at,d.created_at,
                  d.content_status,d.content_hash,m.match_type,m.similarity,m.propagation_role,
                  COALESCE(s.groups,'[]'::jsonb) groups,
                  COALESCE(s.audiences,'[]'::jsonb) audiences,
                  COALESCE(s.country_spaces,'[]'::jsonb) country_spaces
             FROM primary_info_cluster_members_v1 m
             JOIN source_documents d ON d.id=m.source_document_id
        LEFT JOIN sources s ON s.id=d.source_id
            WHERE m.cluster_id=?
            ORDER BY COALESCE(NULLIF(d.published_at,'')::timestamptz,
                              NULLIF(d.created_at,'')::timestamptz),d.id""",
        (cluster_id,),
    ).fetchall()
    if not members:
        return

    old_canonical_id = int(cluster["canonical_document_id"])
    canonical_id = choose_canonical(
        connection, cluster_id, old_canonical_id, members
    )
    if canonical_id != old_canonical_id:
        reseat_canonical(
            connection, cluster_id, old_canonical_id, canonical_id, members
        )
        members = connection.execute(
            """SELECT d.id,d.source_id,d.source_kind,d.source_title,d.source_url,
                      d.source_text,d.feed_text,d.full_text,d.published_at,d.created_at,
                      d.content_status,d.content_hash,m.match_type,m.similarity,m.propagation_role,
                      COALESCE(s.groups,'[]'::jsonb) groups,
                      COALESCE(s.audiences,'[]'::jsonb) audiences,
                      COALESCE(s.country_spaces,'[]'::jsonb) country_spaces
                 FROM primary_info_cluster_members_v1 m
                 JOIN source_documents d ON d.id=m.source_document_id
            LEFT JOIN sources s ON s.id=d.source_id
                WHERE m.cluster_id=?
                ORDER BY COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                  NULLIF(d.created_at,'')::timestamptz),d.id""",
            (cluster_id,),
        ).fetchall()

    groups = sorted({x for row in members for x in (row["groups"] or [])})
    audiences = sorted({x for row in members for x in (row["audiences"] or [])})
    spaces = sorted({x for row in members for x in (row["country_spaces"] or [])})

    source_keys = {
        row["source_id"] if row["source_id"] is not None
        else f"{row['source_kind']}::{row['source_title']}"
        for row in members
    }
    unique_sources = max(1, len(source_keys))

    non_tg_domains = {
        source_domain(row["source_url"])
        for row in members
        if str(row["source_kind"] or "").lower() != "telegram"
        and source_domain(row["source_url"])
    }
    telegram_keys = {
        row["source_id"] if row["source_id"] is not None else row["source_title"]
        for row in members
        if str(row["source_kind"] or "").lower() == "telegram"
    }
    kinds = {
        str(row["source_kind"] or "").lower()
        for row in members if row["source_kind"]
    }
    cross_platform = len(kinds) >= 2 and unique_sources >= 2

    representatives = []
    seen = set()
    canonical = next(
        (row for row in members if int(row["id"]) == canonical_id),
        members[0],
    )
    ordered_members = [canonical] + [
        row for row in members if int(row["id"]) != canonical_id
    ]
    for row in ordered_members:
        key = row["source_id"] or (
            str(row["source_kind"]) + "::" + str(row["source_title"])
        )
        if key in seen:
            continue
        seen.add(key)
        representatives.append({
            "document_id": int(row["id"]),
            "source_id": row["source_id"],
            "kind": row["source_kind"],
            "title": row["source_title"],
            "url": row["source_url"],
            "canonical": int(row["id"]) == canonical_id,
        })
        if len(representatives) >= 25:
            break

    analysis_document = choose_analysis_document(canonical_id, members)
    analysis_document_id = int(analysis_document["id"])

    connection.execute(
        """UPDATE primary_info_clusters_v1
              SET canonical_document_id=?,analysis_document_id=?,spread_count=?,unique_sources=?,
                  unique_domains=?,unique_telegram_channels=?,cross_platform=?,
                  first_seen_at=(
                    SELECT min(COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                        NULLIF(d.created_at,'')::timestamptz))
                      FROM primary_info_cluster_members_v1 mm
                      JOIN source_documents d ON d.id=mm.source_document_id
                     WHERE mm.cluster_id=?
                  ),
                  last_seen_at=(
                    SELECT max(COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                        NULLIF(d.created_at,'')::timestamptz))
                      FROM primary_info_cluster_members_v1 mm
                      JOIN source_documents d ON d.id=mm.source_document_id
                     WHERE mm.cluster_id=?
                  ),
                  source_groups=?::jsonb,audiences=?::jsonb,country_spaces=?::jsonb,
                  representative_sources=?::jsonb,classifier_version=?,updated_at=now()
            WHERE id=?""",
        (
            canonical_id, analysis_document_id, len(members), unique_sources,
            max(1, len(non_tg_domains)) if non_tg_domains else 0,
            len(telegram_keys), cross_platform,
            cluster_id, cluster_id,
            json_value(groups), json_value(audiences), json_value(spaces),
            json_value(representatives), VERSION, cluster_id,
        ),
    )

    text = info_prefilter.select_prefilter_text(
        analysis_document["source_text"], analysis_document["full_text"], analysis_document["feed_text"]
    )
    title = (
        analysis_document["feed_text"]
        or canonical["feed_text"]
        or ""
    )[:800]

    base_dynamic_terms = dynamic_terms_for_member(connection, analysis_document)
    result = info_prefilter.classify(
        text,
        title=title,
        source_title=canonical["source_title"] or "",
        source_kind=canonical["source_kind"] or "",
        spread_count=len(members),
        unique_sources=unique_sources,
        cross_platform=cross_platform,
        dynamic_terms=base_dynamic_terms,
        source_groups=analysis_document["groups"] or (),
    )

    signal_document_id = canonical_id
    best_signal_score = (-1, -1, -1)
    if str(analysis_document["source_kind"] or "").lower() == "telegram":
        signal_document_id = analysis_document_id
        best_signal_score = (
            ROUTE_RANK.get(result.route, 0),
            result.influence_score,
            result.domain_score,
        )

    for member in members:
        if member["match_type"] == "local_junk":
            continue
        if str(member["source_kind"] or "").lower() != "telegram":
            continue
        if int(member["id"]) == analysis_document_id:
            continue
        member_text = info_prefilter.select_prefilter_text(member["source_text"], member["full_text"], member["feed_text"])
        if not member_text:
            continue
        member_dynamic_terms = dynamic_terms_for_member(connection, member)
        member_result = info_prefilter.classify(
            member_text,
            title=(member["feed_text"] or "")[:800],
            source_title=member["source_title"] or "",
            source_kind=member["source_kind"] or "",
            spread_count=1,
            unique_sources=1,
            cross_platform=False,
            dynamic_terms=member_dynamic_terms,
            source_groups=member["groups"] or (),
        )
        member_score = (
            ROUTE_RANK.get(member_result.route, 0),
            member_result.influence_score,
            member_result.domain_score,
        )
        if member_score > best_signal_score:
            best_signal_score = member_score
            signal_document_id = int(member["id"])
        if (
            ROUTE_RANK.get(member_result.route, 0) > ROUTE_RANK.get(result.route, 0)
            or member_result.influence_score > result.influence_score
            or member_dynamic_terms
        ):
            result = merge_prefilter_results(
                result, member_result, int(member["id"])
            )

    connection.execute(
        """UPDATE primary_info_clusters_v1
              SET signal_document_id=?,updated_at=now()
            WHERE id=?""",
        (signal_document_id, cluster_id),
    )

    connection.execute(
        """INSERT INTO info_prefilter_results_v1(
               source_document_id,cluster_id,canonical_document_id,route,hard_filter_reason,
               domain_score,influence_score,spread_score,actor_hits,narrative_hits,
               hashtag_hits,reason_codes,rules_version,processed_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?::jsonb,?::jsonb,?::jsonb,?::jsonb,?,now(),now())
           ON CONFLICT(source_document_id) DO UPDATE SET
             cluster_id=excluded.cluster_id,
             canonical_document_id=excluded.canonical_document_id,
             route=excluded.route,
             hard_filter_reason=excluded.hard_filter_reason,
             domain_score=excluded.domain_score,
             influence_score=excluded.influence_score,
             spread_score=excluded.spread_score,
             actor_hits=excluded.actor_hits,
             narrative_hits=excluded.narrative_hits,
             hashtag_hits=excluded.hashtag_hits,
             reason_codes=excluded.reason_codes,
             rules_version=excluded.rules_version,
             processed_at=excluded.processed_at,
             updated_at=now()""",
        (
            canonical_id, cluster_id, canonical_id, result.route,
            result.hard_filter_reason, result.domain_score,
            result.influence_score, result.spread_score,
            json_value(result.actor_hits), json_value(result.narrative_hits),
            json_value(result.hashtag_hits), json_value(result.reason_codes),
            VERSION,
        ),
    )

    for row in members:
        did = int(row["id"])
        if did == canonical_id:
            continue
        duplicate_route = (
            "DROP_HARD" if row["match_type"] == "local_junk" else "DUPLICATE"
        )
        duplicate_reason = (
            "local junk duplicate/member"
            if duplicate_route == "DROP_HARD"
            else f"{row['match_type']} copy of canonical {canonical_id}"
        )
        connection.execute(
            """INSERT INTO info_prefilter_results_v1(
                   source_document_id,cluster_id,canonical_document_id,route,hard_filter_reason,
                   domain_score,influence_score,spread_score,actor_hits,narrative_hits,
                   hashtag_hits,reason_codes,rules_version,processed_at,updated_at)
               VALUES(?,?,?,?,?,0,0?, '[]'::jsonb,'[]'::jsonb,'[]'::jsonb,?::jsonb,?,now(),now())
               ON CONFLICT(source_document_id) DO UPDATE SET
                 cluster_id=excluded.cluster_id,
                 canonical_document_id=excluded.canonical_document_id,
                 route=excluded.route,
                 hard_filter_reason=excluded.hard_filter_reason,
                 domain_score=0,influence_score=0,
                 spread_score=excluded.spread_score,
                 actor_hits='[]'::jsonb,narrative_hits='[]'::jsonb,
                 hashtag_hits='[]'::jsonb,
                 reason_codes=excluded.reason_codes,
                 rules_version=excluded.rules_version,
                 processed_at=excluded.processed_at,
                 updated_at=now()""".replace("0,0?", "0,0,?"),
            (
                did, cluster_id, canonical_id, duplicate_route,
                row["match_type"] if duplicate_route == "DROP_HARD" else "",
                result.spread_score, json_value((duplicate_reason,)), VERSION,
            ),
        )
        connection.execute(
            """UPDATE primary_info_queue_v1 q
                  SET status='clustered',decision='INHERIT',
                      canonical_document_id=?,batch_id='',locked_at=NULL,retry_at=NULL,
                      reason=?,completed_at=now(),updated_at=now()
                WHERE q.source_document_id=?
                  AND NOT EXISTS(
                      SELECT 1 FROM primary_info_results_v1 r
                       WHERE r.source_document_id=q.source_document_id
                  )""",
            (canonical_id, duplicate_reason, did),
        )

    already_analyzed = connection.execute(
        "SELECT 1 FROM primary_info_results_v1 WHERE source_document_id=?",
        (canonical_id,),
    ).fetchone()
    if already_analyzed:
        return

    if result.route in {"MUST_ANALYZE", "ANALYZE"}:
        connection.execute(
            """INSERT INTO primary_info_queue_v1(
                   source_document_id,cluster_id,canonical_document_id,status,decision,
                   classifier_version,reason,completed_at,updated_at)
               VALUES(?,?,?,'pending','',?,?,NULL,now())
               ON CONFLICT(source_document_id) DO UPDATE SET
                 cluster_id=excluded.cluster_id,
                 canonical_document_id=excluded.canonical_document_id,
                 status='pending',decision='',batch_id='',locked_at=NULL,retry_at=NULL,
                 classifier_version=excluded.classifier_version,reason=excluded.reason,
                 completed_at=NULL,updated_at=now()
               WHERE NOT EXISTS(
                   SELECT 1 FROM primary_info_results_v1 r
                    WHERE r.source_document_id=primary_info_queue_v1.source_document_id
               )""",
            (
                canonical_id, cluster_id, canonical_id, VERSION,
                "; ".join(result.reason_codes)[:1500],
            ),
        )
    else:
        decision = "PREFILTER_HOLD" if result.route == "HOLD" else "PREFILTER_DROP"
        connection.execute(
            """INSERT INTO primary_info_queue_v1(
                   source_document_id,cluster_id,canonical_document_id,status,decision,
                   classifier_version,reason,completed_at,updated_at)
               VALUES(?,?,?,'completed',?,?,?,now(),now())
               ON CONFLICT(source_document_id) DO UPDATE SET
                 cluster_id=excluded.cluster_id,
                 canonical_document_id=excluded.canonical_document_id,
                 status='completed',decision=excluded.decision,batch_id='',locked_at=NULL,
                 retry_at=NULL,classifier_version=excluded.classifier_version,
                 reason=excluded.reason,completed_at=now(),updated_at=now()
               WHERE NOT EXISTS(
                 SELECT 1 FROM primary_info_results_v1 r
                  WHERE r.source_document_id=primary_info_queue_v1.source_document_id
               )""",
            (
                canonical_id, cluster_id, canonical_id, decision, VERSION,
                "; ".join(result.reason_codes)[:1500],
            ),
        )

def main():
    started = time.monotonic()
    connection = db.connect(readonly=False)
    affected = {}  # insertion-ordered cluster ids: live rows stay ahead of backlog
    stats = {"new": 0, "junk": 0, "exact": 0, "near": 0, "canonical": 0}
    try:
        rows = connection.execute(
            """WITH candidates AS (
                 SELECT d.id,d.source_id,d.source_kind,d.source_title,d.source_url,
                        d.source_text,d.feed_text,d.full_text,d.published_at,d.created_at,
                        d.content_hash,d.content_status,
                        COALESCE(s.groups,'[]'::jsonb) groups,
                        COALESCE(s.audiences,'[]'::jsonb) audiences,
                        COALESCE(s.country_spaces,'[]'::jsonb) country_spaces,
                        NULLIF(d.created_at,'')::timestamptz AS created_ts,
                        CASE COALESCE(pf.route,'')
                          WHEN 'MUST_ANALYZE' THEN 0
                          WHEN 'ANALYZE' THEN 1
                          WHEN 'HOLD' THEN 2
                          WHEN 'DROP_SOFT' THEN 3
                          WHEN 'DROP_HARD' THEN 4
                          ELSE 5
                        END AS route_priority
                   FROM source_documents d
              LEFT JOIN sources s ON s.id=d.source_id
              LEFT JOIN info_prefilter_results_v1 pf ON pf.source_document_id=d.id
                  WHERE NULLIF(d.created_at,'')::timestamptz >= ?::timestamptz
                    AND NOT EXISTS(
                      SELECT 1 FROM primary_info_cluster_members_v1 m
                       WHERE m.source_document_id=d.id
                    )
               ),
               live AS (
                 SELECT *,0 AS lane
                   FROM candidates
                  WHERE created_ts >= now()-(? || ' hours')::interval
                  ORDER BY route_priority ASC,created_ts DESC,id DESC
                  LIMIT ?
               ),
               backlog AS (
                 SELECT *,1 AS lane
                   FROM candidates
                  WHERE created_ts < now()-(? || ' hours')::interval
                  ORDER BY route_priority ASC,created_ts ASC,id ASC
                  LIMIT ?
               )
               SELECT * FROM live
               UNION ALL
               SELECT * FROM backlog
               ORDER BY lane,route_priority,created_ts DESC,id DESC""",
            (CUTOFF, LIVE_LOOKBACK_HOURS, LIVE_BATCH_SIZE,
             LIVE_LOOKBACK_HOURS, BACKLOG_BATCH_SIZE),
        ).fetchall()
        stats["new"] = len(rows)
        stats["live_batch"] = sum(1 for row in rows if int(row["lane"]) == 0)
        stats["backlog_batch"] = sum(1 for row in rows if int(row["lane"]) == 1)

        for row in rows:
            did = int(row["id"])
            text = (row["full_text"] or row["source_text"] or "").strip()
            junk = primary_dedup.quick_junk_reason(text)
            if junk:
                cid = ensure_cluster(connection, row)
                affected[cid] = None
                connection.execute(
                    """INSERT INTO primary_info_cluster_members_v1(
                           cluster_id,source_document_id,match_type,similarity,propagation_role)
                       VALUES(?,?,'local_junk',1.0,'origin')
                       ON CONFLICT(source_document_id) DO NOTHING""",
                    (cid, did),
                )
                stats["junk"] += 1
                continue

            exact_hours = exact_window_hours(text)
            exact = connection.execute(
                """SELECT m.cluster_id,cl.canonical_document_id
                     FROM source_documents d2
                     JOIN primary_info_cluster_members_v1 m ON m.source_document_id=d2.id
                     JOIN primary_info_clusters_v1 cl ON cl.id=m.cluster_id
                    WHERE d2.id<>? AND d2.content_hash=?
                      AND m.match_type<>'local_junk'
                      AND abs(extract(epoch from (
                          COALESCE(NULLIF(d2.published_at,'')::timestamptz,
                                   NULLIF(d2.created_at,'')::timestamptz)
                          - ?::timestamptz))) <= ?*3600
                    ORDER BY abs(extract(epoch from (
                          COALESCE(NULLIF(d2.published_at,'')::timestamptz,
                                   NULLIF(d2.created_at,'')::timestamptz)
                          - ?::timestamptz))),d2.id
                    LIMIT 1""",
                (did, row["content_hash"], ts(row), exact_hours, ts(row)),
            ).fetchone()
            if exact:
                cid = int(exact["cluster_id"])
                affected[cid] = None
                connection.execute(
                    """INSERT INTO primary_info_cluster_members_v1(
                           cluster_id,source_document_id,match_type,similarity,propagation_role)
                       VALUES(?,?,'exact',1.0,'repeater')
                       ON CONFLICT(source_document_id) DO NOTHING""",
                    (cid, did),
                )
                stats["exact"] += 1
                continue

            best = None
            if len(primary_dedup.meaningful_tokens(text)) >= 30 and len(primary_dedup.normalize_text(text)) >= 220:
                min_chars = max(1, int(len(text) * 0.70))
                max_chars = max(min_chars + 1, int(len(text) * 1.40) + 200)
                candidates = connection.execute(
                    """SELECT d2.id,d2.source_text,d2.full_text,m.cluster_id,cl.canonical_document_id
                         FROM primary_info_clusters_v1 cl
                         JOIN primary_info_cluster_members_v1 m
                           ON m.cluster_id=cl.id
                          AND m.source_document_id=cl.canonical_document_id
                          AND m.match_type='canonical'
                         JOIN source_documents d2 ON d2.id=cl.canonical_document_id
                        WHERE cl.last_seen_at >= ?::timestamptz - interval '36 hours'
                          AND cl.first_seen_at <= ?::timestamptz + interval '36 hours'
                          AND length(trim(COALESCE(NULLIF(d2.full_text,''),d2.source_text,'')))
                              BETWEEN ? AND ?
                          AND abs(extract(epoch from (
                              COALESCE(NULLIF(d2.published_at,'')::timestamptz,
                                       NULLIF(d2.created_at,'')::timestamptz)
                              - ?::timestamptz))) <= 36*3600
                        ORDER BY abs(extract(epoch from (
                              COALESCE(NULLIF(d2.published_at,'')::timestamptz,
                                       NULLIF(d2.created_at,'')::timestamptz)
                              - ?::timestamptz)))
                        LIMIT 120""",
                    (ts(row), ts(row), min_chars, max_chars, ts(row), ts(row)),
                ).fetchall()
                for candidate in candidates:
                    candidate_text = (candidate["full_text"] or candidate["source_text"] or "").strip()
                    sim = primary_dedup.near_similarity(text, candidate_text)
                    if sim >= 0.92 and (best is None or sim > best[0]):
                        best = (sim, candidate)

            if best:
                sim, candidate = best
                cid = int(candidate["cluster_id"])
                affected[cid] = None
                connection.execute(
                    """INSERT INTO primary_info_cluster_members_v1(
                           cluster_id,source_document_id,match_type,similarity,propagation_role)
                       VALUES(?,?,'near',?,'repeater')
                       ON CONFLICT(source_document_id) DO NOTHING""",
                    (cid, did, float(sim)),
                )
                stats["near"] += 1
                continue

            cid = ensure_cluster(connection, row)
            affected[cid] = None
            connection.execute(
                """INSERT INTO primary_info_cluster_members_v1(
                       cluster_id,source_document_id,match_type,similarity,propagation_role)
                   VALUES(?,?,'canonical',1.0,'origin')
                   ON CONFLICT(source_document_id) DO NOTHING""",
                (cid, did),
            )
            stats["canonical"] += 1

        connection.commit()

        for cid in affected:
            update_cluster_and_prefilter(connection, cid)
        connection.commit()

        stats["pending_analysis"] = int(connection.execute(
            "SELECT count(*) AS n FROM primary_info_queue_v1 WHERE status='pending'"
        ).fetchone()["n"])
        stats["prefiltered"] = int(connection.execute(
            "SELECT count(*) AS n FROM info_prefilter_results_v1"
        ).fetchone()["n"])
        stats["at"] = datetime.now(timezone.utc).isoformat()
        stats["status"] = "ok"
        stats["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_run_log(stats)
        print(json.dumps(stats, ensure_ascii=False))
    finally:
        connection.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        write_run_log({
            "at": datetime.now(timezone.utc).isoformat(),
            "status": "error",
            "error": repr(exc),
            "traceback": traceback.format_exc()[-12000:],
        })
        raise

