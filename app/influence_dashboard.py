"""Curated information-influence dashboard.

The UI reads Direction/Narrative/Thesis/Story semantics only from the curated
layer. Raw Grok tables are used only for document provenance, source roles and
speaker metadata.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
SCOPE_START = datetime(2026, 9, 21, tzinfo=KYIV).astimezone(timezone.utc)
DOC_OCCURRED = (
    "COALESCE(NULLIF(d.published_at,'')::timestamptz,"
    "NULLIF(d.created_at,'')::timestamptz,a.created_at,s.first_seen_at)"
)


AI_SCOPE_START = datetime(2026, 9, 28, tzinfo=KYIV).astimezone(timezone.utc)


def _speaker_expr(column):
    value = f"lower(trim(COALESCE({column},'')))"
    return f"""CASE
      WHEN {value} IN ('путін','в. путін','володимир путін','путин','владимир путин','vladimir putin') THEN 'Володимир Путін'
      WHEN {value} IN ('пєсков','дмитро пєсков','песков','дмитрий песков') THEN 'Дмитро Пєсков'
      WHEN {value} IN ('захарова','марія захарова','мария захарова') THEN 'Марія Захарова'
      WHEN {value} IN ('лавров','сергій лавров','сергей лавров') THEN 'Сергій Лавров'
      WHEN {value} IN ('міноборони рф','мінобороні рф','минобороны рф','министерство обороны рф') THEN 'Міноборони РФ'
      ELSE trim(COALESCE({column},'')) END"""


def boundaries(period="all", date_from="", date_to="", now=None):
    now = now or datetime.now(timezone.utc)
    if period == "custom" and date_from:
        start = datetime.fromisoformat(date_from).replace(tzinfo=KYIV).astimezone(timezone.utc)
        end = now
        if date_to:
            end = (datetime.fromisoformat(date_to) + timedelta(days=1)).replace(tzinfo=KYIV).astimezone(timezone.utc)
        return max(start, SCOPE_START), min(end, now)
    if period == "all":
        return SCOPE_START, now
    hours = {"day": 24, "48h": 48, "week": 24 * 7}.get(period, 24 * 7)
    return max(now - timedelta(hours=hours), SCOPE_START), now


def _scope_cte(start, end, audience="", speaker="", direction="", narrative=""):
    where = [f"{DOC_OCCURRED}>=?", f"{DOC_OCCURRED}<?", "c.included=true"]
    params = [start, end]
    if audience:
        where.append("EXISTS (SELECT 1 FROM jsonb_array_elements_text(c.audience_context) x(value) WHERE x.value=?)")
        params.append(audience)
    if speaker:
        where.append(f"""EXISTS (
            SELECT 1 FROM grok_story_members_v1 sx
            JOIN grok_document_analysis_v1 ax ON ax.source_document_id=sx.source_document_id
            WHERE sx.story_id=s.id AND ax.speaker_role='speaker' AND {_speaker_expr("ax.speaker") }=?
        )""")
        params.append(speaker)
    if direction:
        where.append("""EXISTS (
            SELECT 1 FROM influence_story_theses_curated_v1 stx
            JOIN influence_theses_curated_v1 tx ON tx.id=stx.thesis_id
            JOIN influence_narratives_curated_v1 nx ON nx.id=tx.narrative_id
            JOIN influence_directions_v1 dx ON dx.id=nx.direction_id
            WHERE stx.story_id=s.id AND dx.direction_key=?
        )""")
        params.append(direction)
    if narrative:
        where.append("""EXISTS (
            SELECT 1 FROM influence_story_theses_curated_v1 stx
            JOIN influence_theses_curated_v1 tx ON tx.id=stx.thesis_id
            JOIN influence_narratives_curated_v1 nx ON nx.id=tx.narrative_id
            WHERE stx.story_id=s.id AND nx.narrative_key=?
        )""")
        params.append(narrative)

    semantic_where = []
    if direction:
        semantic_where.append("dx.direction_key=?")
        params.append(direction)
    if narrative:
        semantic_where.append("nx.narrative_key=?")
        params.append(narrative)
    semantic_filter = ("WHERE " + " AND ".join(semantic_where)) if semantic_where else ""

    speaker_sql = _speaker_expr("a.speaker")
    cte = f"""WITH member_scope AS (
        SELECT sm.story_id,sm.source_document_id,sm.relation,d.source_title,d.source_url,
               {DOC_OCCURRED} AS occurred,
               COALESCE(NULLIF(a.source_role,''),'unclear') AS source_role,
               {speaker_sql} AS speaker,COALESCE(a.speaker_role,'') AS speaker_role
        FROM grok_story_members_v1 sm
        JOIN grok_stories_v1 s ON s.id=sm.story_id
        JOIN influence_story_curation_v1 c ON c.story_id=s.id
        JOIN source_documents d ON d.id=sm.source_document_id
        LEFT JOIN grok_document_analysis_v1 a ON a.source_document_id=sm.source_document_id
        WHERE {' AND '.join(where)}
    ), story_scope AS (
        SELECT DISTINCT story_id FROM member_scope
    ), filtered_links AS (
        SELECT st.story_id,st.thesis_id,st.relation,st.confidence
        FROM influence_story_theses_curated_v1 st
        JOIN story_scope ss ON ss.story_id=st.story_id
        JOIN influence_theses_curated_v1 tx ON tx.id=st.thesis_id
        JOIN influence_narratives_curated_v1 nx ON nx.id=tx.narrative_id
        JOIN influence_directions_v1 dx ON dx.id=nx.direction_id
        {semantic_filter}
    ), primary_semantic AS (
        SELECT DISTINCT ON (st.story_id)
               st.story_id,st.relation,st.confidence,
               t.id AS thesis_id,t.thesis_key,t.label AS thesis_label,t.statement AS thesis_statement,
               n.id AS narrative_id,n.narrative_key,n.label AS narrative_label,
               d.id AS direction_id,d.direction_key,d.label AS direction_label
        FROM filtered_links st
        JOIN influence_theses_curated_v1 t ON t.id=st.thesis_id
        JOIN influence_narratives_curated_v1 n ON n.id=t.narrative_id
        JOIN influence_directions_v1 d ON d.id=n.direction_id
        ORDER BY st.story_id,
                 CASE st.relation WHEN 'primary' THEN 0 WHEN 'secondary' THEN 1 ELSE 2 END,
                 st.confidence DESC,t.id
    )"""
    return cte, params


def _label_dt(value):
    return value.astimezone(KYIV).strftime("%d.%m %H:%M") if value else "—"


def _filter_url(current, **changes):
    data = dict(current)
    data.update(changes)
    clean = {k: v for k, v in data.items() if v not in (None, "")}
    return "/influence?" + urlencode(clean)


def _trend_result(current, previous):
    current = int(current or 0)
    previous = int(previous or 0)
    if current == previous:
        return {"status": "flat", "label": "без змін", "percent": 0, "current": current, "previous": previous}
    if previous == 0:
        return {"status": "up", "label": "нове зростання", "percent": None, "current": current, "previous": previous}
    percent = round((current - previous) * 100 / previous)
    return {
        "status": "up" if percent > 0 else "down",
        "label": f"{percent:+d}%",
        "percent": percent,
        "current": current,
        "previous": previous,
    }


def _trend_windows(start, end, now=None):
    now = now or datetime.now(timezone.utc)
    anchor = min(end, now)
    if anchor <= start:
        return None
    probe = anchor - timedelta(microseconds=1)
    local_probe = probe.astimezone(KYIV)
    local_day_start = local_probe.replace(hour=0, minute=0, second=0, microsecond=0)
    natural_start = local_day_start.astimezone(timezone.utc)
    current_start = max(start, natural_start)
    if anchor <= current_start:
        return None
    elapsed = anchor - current_start
    current_start_local = current_start.astimezone(KYIV)
    previous_start = (current_start_local - timedelta(days=1)).astimezone(timezone.utc)
    previous_end = previous_start + elapsed
    if previous_end <= SCOPE_START:
        return None
    previous_start = max(previous_start, SCOPE_START)
    if previous_end <= previous_start:
        return None
    return current_start, anchor, previous_start, previous_end


def _window_activity(connection, start, end, audience="", speaker="", direction="", narrative=""):
    cte, params = _scope_cte(start, end, audience, speaker, direction, narrative)
    total = connection.execute(cte + """
        SELECT count(DISTINCT source_document_id)::int AS documents,
               count(DISTINCT story_id)::int AS stories
        FROM member_scope
    """, tuple(params)).fetchone()
    rows = connection.execute(cte + """
        SELECT d.direction_key,count(DISTINCT ms.source_document_id)::int AS documents
        FROM story_scope ss
        JOIN filtered_links st ON st.story_id=ss.story_id
        JOIN influence_theses_curated_v1 t ON t.id=st.thesis_id
        JOIN influence_narratives_curated_v1 n ON n.id=t.narrative_id
        JOIN influence_directions_v1 d ON d.id=n.direction_id
        LEFT JOIN member_scope ms ON ms.story_id=ss.story_id
        GROUP BY d.direction_key
    """, tuple(params)).fetchall()
    return {
        "documents": int(total["documents"] or 0),
        "stories": int(total["stories"] or 0),
        "directions": {r["direction_key"]: int(r["documents"] or 0) for r in rows},
    }


def build(connection, period="all", audience="", speaker="", direction="", narrative="",
          date_from="", date_to="", now=None):
    start, end = boundaries(period, date_from, date_to, now)
    if end <= start:
        raise ValueError("Кінець періоду має бути пізніше початку")
    cte, params = _scope_cte(start, end, audience, speaker, direction, narrative)

    baseline = dict(connection.execute("""
        SELECT
          count(*) FILTER (WHERE curation_status='included')::int AS included,
          count(*) FILTER (WHERE curation_status='excluded')::int AS excluded,
          count(*) FILTER (WHERE curation_status='review')::int AS review
        FROM influence_story_curation_v1
    """).fetchone())
    baseline["uncurated"] = connection.execute("""
        SELECT count(*)::int AS n
        FROM grok_stories_v1 s
        LEFT JOIN influence_story_curation_v1 c ON c.story_id=s.id
        WHERE s.first_seen_at>=? AND c.story_id IS NULL
    """, (SCOPE_START,)).fetchone()["n"]

    run = connection.execute("""
        SELECT run_key,status,version,period_start,period_end,notes
        FROM influence_curation_runs_v1
        WHERE status='validated'
        ORDER BY period_end DESC NULLS LAST, updated_at DESC
        LIMIT 1
    """).fetchone()
    run = dict(run) if run else {}
    run["period_start_label"] = _label_dt(run.get("period_start"))
    run["period_end_label"] = _label_dt(run.get("period_end"))

    metrics = dict(connection.execute(cte + """
        SELECT
          count(DISTINCT ss.story_id)::int AS stories,
          count(DISTINCT ms.source_document_id)::int AS documents,
          count(DISTINCT ms.source_title)::int AS sources,
          count(DISTINCT st.thesis_id)::int AS theses,
          count(DISTINCT n.id)::int AS narratives,
          count(DISTINCT d.id)::int AS directions,
          count(DISTINCT (st.story_id,st.thesis_id))::int AS links,
          count(DISTINCT CASE WHEN ms.relation='duplicate' THEN ms.source_document_id END)::int AS duplicates,
          count(DISTINCT CASE WHEN ms.relation='development' THEN ms.source_document_id END)::int AS developments,
          count(DISTINCT CASE WHEN ms.source_role IN ('relay','aggregator') THEN ms.source_document_id END)::int AS relay_docs
        FROM story_scope ss
        LEFT JOIN member_scope ms ON ms.story_id=ss.story_id
        LEFT JOIN filtered_links st ON st.story_id=ss.story_id
        LEFT JOIN influence_theses_curated_v1 t ON t.id=st.thesis_id
        LEFT JOIN influence_narratives_curated_v1 n ON n.id=t.narrative_id
        LEFT JOIN influence_directions_v1 d ON d.id=n.direction_id
    """, tuple(params)).fetchone())

    direction_rows = [dict(r) for r in connection.execute(cte + """
        SELECT d.id,d.direction_key,d.label,d.description,
          count(DISTINCT n.id)::int AS narratives,
          count(DISTINCT t.id)::int AS theses,
          count(DISTINCT st.story_id)::int AS stories,
          count(DISTINCT ms.source_document_id)::int AS documents,
          count(DISTINCT ms.source_title)::int AS sources,
          max(ms.occurred) AS last_seen
        FROM story_scope ss
        JOIN filtered_links st ON st.story_id=ss.story_id
        JOIN influence_theses_curated_v1 t ON t.id=st.thesis_id
        JOIN influence_narratives_curated_v1 n ON n.id=t.narrative_id
        JOIN influence_directions_v1 d ON d.id=n.direction_id
        LEFT JOIN member_scope ms ON ms.story_id=ss.story_id
        GROUP BY d.id,d.direction_key,d.label,d.description
        ORDER BY stories DESC,documents DESC,d.id
    """, tuple(params)).fetchall()]

    trend = {"available": False, "status": "flat", "label": "?", "current": 0, "previous": 0}
    trend_windows = _trend_windows(start, end, now)
    direction_trends = {}
    if trend_windows:
        cur_start, cur_end, prev_start, prev_end = trend_windows
        current_activity = _window_activity(
            connection, cur_start, cur_end, audience, speaker, direction, narrative
        )
        previous_activity = _window_activity(
            connection, prev_start, prev_end, audience, speaker, direction, narrative
        )
        trend = _trend_result(current_activity["documents"], previous_activity["documents"])
        trend["available"] = True
        trend["current_stories"] = current_activity["stories"]
        trend["previous_stories"] = previous_activity["stories"]
        trend["current_label"] = cur_start.astimezone(KYIV).strftime("%d.%m %H:%M") + " - " + cur_end.astimezone(KYIV).strftime("%H:%M")
        trend["previous_label"] = prev_start.astimezone(KYIV).strftime("%d.%m %H:%M") + " - " + prev_end.astimezone(KYIV).strftime("%H:%M")
        all_direction_keys = set(current_activity["directions"]) | set(previous_activity["directions"])
        for key in all_direction_keys:
            direction_trends[key] = _trend_result(
                current_activity["directions"].get(key, 0),
                previous_activity["directions"].get(key, 0),
            )

    semantic_rows = [dict(r) for r in connection.execute(cte + """
        SELECT d.id AS direction_id,d.direction_key,d.label AS direction_label,d.description AS direction_description,
          n.id AS narrative_id,n.narrative_key,n.label AS narrative_label,n.description AS narrative_description,
          n.analytical_function AS narrative_function,n.primary_audiences AS narrative_audiences,
          t.id AS thesis_id,t.thesis_key,t.label AS thesis_label,t.statement AS thesis_statement,
          t.analytical_function AS thesis_function,t.expected_effect,t.primary_audiences AS thesis_audiences,
          count(DISTINCT st.story_id)::int AS stories,
          count(DISTINCT ms.source_document_id)::int AS documents,
          count(DISTINCT ms.source_title)::int AS sources,
          max(ms.occurred) AS last_seen
        FROM story_scope ss
        JOIN filtered_links st ON st.story_id=ss.story_id
        JOIN influence_theses_curated_v1 t ON t.id=st.thesis_id
        JOIN influence_narratives_curated_v1 n ON n.id=t.narrative_id
        JOIN influence_directions_v1 d ON d.id=n.direction_id
        LEFT JOIN member_scope ms ON ms.story_id=ss.story_id
        GROUP BY d.id,d.direction_key,d.label,d.description,
                 n.id,n.narrative_key,n.label,n.description,n.analytical_function,n.primary_audiences,
                 t.id,t.thesis_key,t.label,t.statement,t.analytical_function,t.expected_effect,t.primary_audiences
        ORDER BY d.id,n.id,stories DESC,t.id
    """, tuple(params)).fetchall()]

    hierarchy_map = {}
    for row in semantic_rows:
        did = row["direction_id"]
        nid = row["narrative_id"]
        direction_node = hierarchy_map.setdefault(did, {
            "id": did, "key": row["direction_key"], "label": row["direction_label"],
            "description": row["direction_description"], "narratives": [], "_n": {},
        })
        narrative_node = direction_node["_n"].get(nid)
        if narrative_node is None:
            narrative_node = {
                "id": nid, "key": row["narrative_key"], "label": row["narrative_label"],
                "description": row["narrative_description"], "function": row["narrative_function"],
                "audiences": row["narrative_audiences"] or [], "theses": [],
            }
            direction_node["_n"][nid] = narrative_node
            direction_node["narratives"].append(narrative_node)

        narrative_node["theses"].append({
            "id": row["thesis_id"], "key": row["thesis_key"], "label": row["thesis_label"],
            "statement": row["thesis_statement"], "function": row["thesis_function"],
            "expected_effect": row["expected_effect"], "audiences": row["thesis_audiences"] or [],
            "stories": row["stories"], "documents": row["documents"], "sources": row["sources"],
            "last_seen": row["last_seen"], "last_seen_label": _label_dt(row["last_seen"]),
        })
    hierarchy = list(hierarchy_map.values())
    for node in hierarchy:
        node.pop("_n", None)

    stories = [dict(r) for r in connection.execute(cte + """
        SELECT s.id AS story_id,s.story_key,s.title,s.summary,
          COALESCE(c.curated_canonical_document_id,s.canonical_document_id) AS canonical_document_id,
          cd.source_title AS canonical_source,cd.source_url AS canonical_url,
          c.audience_context,c.propagation_note,c.chronology_note,c.curation_version,
          ps.direction_key,ps.direction_label,ps.narrative_key,ps.narrative_label,
          ps.thesis_key,ps.thesis_label,ps.thesis_statement,ps.relation,ps.confidence,
          count(DISTINCT ms.source_document_id)::int AS documents,
          count(DISTINCT ms.source_title)::int AS sources,
          count(DISTINCT CASE WHEN ms.relation='duplicate' THEN ms.source_document_id END)::int AS duplicates,
          count(DISTINCT CASE WHEN ms.relation='development' THEN ms.source_document_id END)::int AS developments,
          count(DISTINCT CASE WHEN ms.source_role IN ('relay','aggregator') THEN ms.source_document_id END)::int AS relay_docs,
          max(ms.occurred) AS last_seen,
          string_agg(DISTINCT NULLIF(ms.speaker,''), ', ') FILTER (
             WHERE ms.speaker_role='speaker' AND NULLIF(ms.speaker,'') IS NOT NULL
          ) AS speakers
        FROM story_scope ss
        JOIN grok_stories_v1 s ON s.id=ss.story_id
        JOIN influence_story_curation_v1 c ON c.story_id=s.id
        LEFT JOIN primary_semantic ps ON ps.story_id=s.id
        LEFT JOIN member_scope ms ON ms.story_id=s.id
        LEFT JOIN source_documents cd ON cd.id=COALESCE(c.curated_canonical_document_id,s.canonical_document_id)
        GROUP BY s.id,s.story_key,s.title,s.summary,c.curated_canonical_document_id,s.canonical_document_id,
                 cd.source_title,cd.source_url,c.audience_context,c.propagation_note,c.chronology_note,c.curation_version,
                 ps.direction_key,ps.direction_label,ps.narrative_key,ps.narrative_label,
                 ps.thesis_key,ps.thesis_label,ps.thesis_statement,ps.relation,ps.confidence
        ORDER BY max(ms.occurred) DESC NULLS LAST,documents DESC,s.id DESC LIMIT 24
    """, tuple(params)).fetchall()]

    for row in stories:
        row["last_seen_label"] = _label_dt(row.get("last_seen"))
        row["audience_context"] = row.get("audience_context") or []

    audiences = [dict(r) for r in connection.execute(cte + """
        SELECT x.value,count(DISTINCT c.story_id)::int AS stories
        FROM story_scope ss
        JOIN influence_story_curation_v1 c ON c.story_id=ss.story_id
        CROSS JOIN LATERAL jsonb_array_elements_text(c.audience_context) x(value)
        GROUP BY x.value ORDER BY stories DESC,x.value
    """, tuple(params)).fetchall()]

    speakers = [dict(r) for r in connection.execute(cte + """
        SELECT speaker,count(DISTINCT story_id)::int AS stories,
          count(DISTINCT source_document_id)::int AS documents,
          count(DISTINCT source_title)::int AS sources
        FROM member_scope
        WHERE speaker_role='speaker' AND trim(speaker)<>''
        GROUP BY speaker ORDER BY documents DESC,stories DESC,speaker LIMIT 12
    """, tuple(params)).fetchall()]

    source_roles = [dict(r) for r in connection.execute(cte + """
        SELECT source_role AS value,count(DISTINCT source_document_id)::int AS documents,
          count(DISTINCT story_id)::int AS stories
        FROM member_scope
        GROUP BY source_role ORDER BY documents DESC,value
    """, tuple(params)).fetchall()]

    role_labels = {
        "originator": "першоджерело", "official": "офіційне джерело",
        "relay": "ретранслятор", "aggregator": "агрегатор",
        "commentary": "коментар / інтерпретація", "unclear": "роль не визначена",
    }
    for row in source_roles:
        row["label"] = role_labels.get(row["value"], row["value"])

    relay_sources = [dict(r) for r in connection.execute(cte + """
        SELECT source_title,count(DISTINCT source_document_id)::int AS documents,
          count(DISTINCT story_id)::int AS stories,
          max(occurred) AS last_seen
        FROM member_scope
        WHERE source_role IN ('relay','aggregator')
        GROUP BY source_title ORDER BY documents DESC,stories DESC,source_title LIMIT 10
    """, tuple(params)).fetchall()]
    for row in relay_sources:
        row["last_seen_label"] = _label_dt(row.get("last_seen"))

    grain = "hour" if (end - start) <= timedelta(days=2) else "day"
    timeline = [dict(r) for r in connection.execute(cte + f"""
        SELECT date_trunc('{grain}',occurred AT TIME ZONE 'Europe/Kyiv') AS bucket,
          count(DISTINCT source_document_id)::int AS documents,
          count(DISTINCT story_id)::int AS stories,
          count(DISTINCT CASE WHEN relation='development' THEN source_document_id END)::int AS developments
        FROM member_scope GROUP BY 1 ORDER BY 1
    """, tuple(params)).fetchall()]
    for row in timeline:
        row["label"] = row["bucket"].strftime("%d.%m %H:00" if grain == "hour" else "%d.%m")
    max_timeline = max([r["documents"] for r in timeline] or [1])

    excluded = []
    if not any((direction, narrative, audience, speaker)):
        excluded = [dict(r) for r in connection.execute("""
            SELECT s.id AS story_id,s.title,s.summary,s.canonical_document_id,
              c.exclusion_reason,s.first_seen_at,s.last_seen_at
            FROM influence_story_curation_v1 c
            JOIN grok_stories_v1 s ON s.id=c.story_id
            WHERE c.curation_status='excluded'
              AND s.first_seen_at<? AND COALESCE(s.last_seen_at,s.first_seen_at)>=?
            ORDER BY s.first_seen_at DESC,s.id DESC LIMIT 10
        """, (end, start)).fetchall()]
        for row in excluded:
            row["first_seen_label"] = _label_dt(row.get("first_seen_at"))

    coverage_row = connection.execute("""
        SELECT min(s.first_seen_at) AS first_seen,max(s.last_seen_at) AS last_seen
        FROM influence_story_curation_v1 c
        JOIN grok_stories_v1 s ON s.id=c.story_id
        WHERE c.included=true
    """).fetchone()
    coverage = dict(coverage_row)
    coverage["first_seen_label"] = _label_dt(coverage.get("first_seen"))
    coverage["last_seen_label"] = _label_dt(coverage.get("last_seen"))

    freshness_row = connection.execute("""
        SELECT max(s.last_seen_at) AS raw_last_seen,
               count(*) FILTER (WHERE c.story_id IS NULL)::int AS uncurated_total,
               count(*) FILTER (WHERE c.story_id IS NULL AND s.first_seen_at>=?)::int AS uncurated_ai_scope
        FROM grok_stories_v1 s
        LEFT JOIN influence_story_curation_v1 c ON c.story_id=s.id
        WHERE s.first_seen_at>=?
    """, (AI_SCOPE_START, SCOPE_START)).fetchone()
    raw_last = freshness_row["raw_last_seen"]
    curated_last = coverage.get("last_seen")
    lag_hours = None
    if raw_last is not None and curated_last is not None:
        lag_hours = max(0, round((raw_last - curated_last).total_seconds() / 3600, 1))
    freshness = {
        "raw_last_seen": raw_last, "raw_last_seen_label": _label_dt(raw_last),
        "curated_last_seen": curated_last, "curated_last_seen_label": _label_dt(curated_last),
        "uncurated_total": int(freshness_row["uncurated_total"] or 0),
        "uncurated_ai_scope": int(freshness_row["uncurated_ai_scope"] or 0),
        "lag_hours": lag_hours,
        "stale": bool((lag_hours or 0) >= 6 or int(freshness_row["uncurated_ai_scope"] or 0) > 0),
    }
    baseline["uncurated_ai_scope"] = freshness["uncurated_ai_scope"]

    available_directions = [dict(r) for r in connection.execute("""
        SELECT direction_key,label FROM influence_directions_v1
        WHERE status='active' ORDER BY id
    """).fetchall()]
    if direction:
        available_narratives = [dict(r) for r in connection.execute("""
            SELECT n.narrative_key,n.label
            FROM influence_narratives_curated_v1 n
            JOIN influence_directions_v1 d ON d.id=n.direction_id
            WHERE n.status='active' AND d.direction_key=? ORDER BY n.id
        """, (direction,)).fetchall()]
    else:
        available_narratives = [dict(r) for r in connection.execute("""
            SELECT n.narrative_key,n.label
            FROM influence_narratives_curated_v1 n
            WHERE n.status='active' ORDER BY n.id
        """).fetchall()]
    available_audiences = [r["value"] for r in connection.execute("""
        SELECT value,count(*)::int n
        FROM influence_story_curation_v1 c
        CROSS JOIN LATERAL jsonb_array_elements_text(c.audience_context) x(value)
        WHERE c.included=true GROUP BY value ORDER BY n DESC,value
    """).fetchall()]
    available_speaker_sql = _speaker_expr("a.speaker")
    available_speakers = [r["speaker"] for r in connection.execute(f"""
        SELECT {available_speaker_sql} AS speaker,count(DISTINCT sm.story_id)::int n
        FROM grok_document_analysis_v1 a
        JOIN grok_story_members_v1 sm ON sm.source_document_id=a.source_document_id
        JOIN influence_story_curation_v1 c ON c.story_id=sm.story_id AND c.included=true
        WHERE a.speaker_role='speaker' AND trim(COALESCE(a.speaker,''))<>''
        GROUP BY 1 ORDER BY n DESC,1
    """).fetchall()]

    current_filters = {
        "period": period, "audience": audience, "speaker": speaker,
        "direction": direction, "narrative": narrative,
        "date_from": date_from, "date_to": date_to,
    }

    for row in direction_rows:
        row["last_seen_label"] = _label_dt(row.get("last_seen"))
        row["filter_url"] = _filter_url(current_filters, direction=row["direction_key"], narrative="")
        row["selected"] = row["direction_key"] == direction
        row["trend"] = direction_trends.get(row["direction_key"], _trend_result(0, 0))
    for node in hierarchy:
        node["selected"] = node["key"] == direction
        node["filter_url"] = _filter_url(current_filters, direction=node["key"], narrative="")
        for narr in node["narratives"]:
            narr["selected"] = narr["key"] == narrative
            narr["filter_url"] = _filter_url(
                current_filters, direction=node["key"], narrative=narr["key"]
            )

    selected_direction = next((r["label"] for r in available_directions if r["direction_key"] == direction), "")
    selected_narrative = next((r["label"] for r in available_narratives if r["narrative_key"] == narrative), "")

    return {
        "period": period, "audience": audience, "speaker": speaker,
        "direction": direction, "narrative": narrative,
        "date_from": date_from, "date_to": date_to,
        "start": start, "end": end,
        "metrics": metrics, "baseline": baseline, "run": run, "coverage": coverage, "freshness": freshness, "trend": trend,
        "directions": direction_rows, "hierarchy": hierarchy, "stories": stories,
        "audiences": audiences, "speakers": speakers, "source_roles": source_roles,
        "relay_sources": relay_sources, "timeline": timeline, "max_timeline": max_timeline,
        "excluded": excluded,
        "available_directions": available_directions,
        "available_narratives": available_narratives,
        "available_audiences": available_audiences,
        "available_speakers": available_speakers,
        "selected_direction": selected_direction, "selected_narrative": selected_narrative,
    }
