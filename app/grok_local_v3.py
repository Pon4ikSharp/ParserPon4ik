from __future__ import annotations

import json
import math
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import db
from app import hold_v2
from app import story_propagation

ROOT = db.ROOT
VERSION = "grok-document-v3.3-20260928"
SCOPE_START = "2026-09-28T00:00:00"
ROUTES = ("MUST_ANALYZE", "ANALYZE")
AUDIENCES = {
    "Населення РФ",
    "Українське суспільство",
    "Населення ТОТ України",
    "Аудиторії країн Європи",
    "Міжнародна аудиторія",
    "Російська діаспора",
}
STOP = {
    "the", "and", "for", "with", "that", "this", "from", "что", "это", "как",
    "для", "при", "или", "его", "она", "они", "про", "який", "яка", "які",
    "цей", "ця", "було", "була", "були", "также",
}


def _tokens(text: str) -> set[str]:
    normalized = re.sub(r"[^0-9A-Za-zА-Яа-яІіЇїЄєҐґ]+", " ", text or "").lower()
    return {x for x in normalized.split() if len(x) >= 3 and not x.isdigit() and x not in STOP}


def _dice(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return (2.0 * len(left & right)) / (len(left) + len(right))


def _text(row: dict[str, Any], limit: int = 6500) -> str:
    for key in ("full_text", "source_text", "feed_text"):
        value = str(row.get(key) or "").strip()
        if value:
            return value[:limit]
    return ""


def backlog_counts(connection: db.Connection) -> dict[str, int]:
    rows = connection.execute(
        """
        SELECT p.route,count(*)::int AS n
          FROM info_prefilter_results_v1 p
          JOIN source_documents d ON d.id=p.source_document_id
     LEFT JOIN grok_document_analysis_v1 g ON g.source_document_id=d.id
     LEFT JOIN hold_v2_results_v1 h ON h.source_document_id=d.id
         WHERE p.route IN ('MUST_ANALYZE','ANALYZE')
           AND g.source_document_id IS NULL
           AND (p.route<>'HOLD' OR h.disposition='PROMOTE_TO_AI')
           AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                        NULLIF(d.created_at,'')::timestamptz)
               >= (?::timestamp AT TIME ZONE 'Europe/Kyiv')
           AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                        NULLIF(d.created_at,'')::timestamptz)
               <= now()+interval '5 minutes'
         GROUP BY p.route
        """,
        (SCOPE_START,),
    ).fetchall()
    return {str(r["route"]): int(r["n"]) for r in rows}


def _eligible_rows(connection: db.Connection, route: str, limit: int, *, oldest: bool = False) -> list[dict[str, Any]]:
    hold_order = """
      CASE
        WHEN EXISTS (SELECT 1 FROM jsonb_array_elements_text(p.reason_codes) x WHERE x LIKE 'weekly_relevance:%') THEN 0
        WHEN p.influence_score>=2 OR p.spread_score>=3 THEN 1
        WHEN jsonb_array_length(COALESCE(p.hashtag_hits,'[]'::jsonb))>0 OR p.domain_score>=14 THEN 2
        ELSE 3
      END,
      ts DESC,d.id DESC
    """
    if route == "HOLD":
        order_sql = hold_order.replace("ts DESC,d.id DESC", "ts ASC,d.id ASC") if oldest else hold_order
    else:
        order_sql = "ts ASC,d.id ASC" if oldest else "ts DESC,d.id DESC"
    rows = connection.execute(
        f"""
        SELECT d.id,p.route,p.cluster_id,p.canonical_document_id,
               p.hard_filter_reason,p.domain_score,p.influence_score,p.spread_score,
               p.actor_hits,p.narrative_hits,p.hashtag_hits,p.reason_codes,
               COALESCE(NULLIF(d.published_at,'')::timestamptz,
                        NULLIF(d.created_at,'')::timestamptz) AS ts
          FROM info_prefilter_results_v1 p
          JOIN source_documents d ON d.id=p.source_document_id
     LEFT JOIN grok_document_analysis_v1 g ON g.source_document_id=d.id
     LEFT JOIN hold_v2_results_v1 h ON h.source_document_id=d.id
         WHERE p.route=? AND g.source_document_id IS NULL
           AND (p.route<>'HOLD' OR h.disposition='PROMOTE_TO_AI')
           AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                        NULLIF(d.created_at,'')::timestamptz)
               >= (?::timestamp AT TIME ZONE 'Europe/Kyiv')
           AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                        NULLIF(d.created_at,'')::timestamptz)
               <= now()+interval '5 minutes'
         ORDER BY {order_sql}
         LIMIT ?
        """,
        (route, SCOPE_START, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def _dynamic_route_targets(counts: dict[str, int], target: int) -> dict[str, int]:
    active = [r for r in ROUTES if int(counts.get(r, 0)) > 0]
    if not active:
        return {r: 0 for r in ROUTES}
    floor = max(10, int(round(target * 0.12)))
    alloc = {r: min(int(counts.get(r, 0)), floor) for r in active}
    remaining = max(0, target - sum(alloc.values()))
    weights = {"MUST_ANALYZE": 3.0, "ANALYZE": 2.0, "HOLD": 1.0}
    scores = {r: weights[r] * math.sqrt(max(1, int(counts.get(r, 0)))) for r in active}
    total_score = sum(scores.values()) or 1.0
    for r in active:
        room = max(0, int(counts.get(r, 0)) - alloc[r])
        add = min(room, int(round(remaining * scores[r] / total_score)))
        alloc[r] += add
    return {r: alloc.get(r, 0) for r in ROUTES}


def select_backlog(connection: db.Connection, target: int) -> list[dict[str, Any]]:
    target = max(1, min(int(target), 500))
    counts = backlog_counts(connection)
    route_targets = _dynamic_route_targets(counts, target)
    selected = []
    for route in ROUTES:
        budget = int(route_targets.get(route, 0))
        if budget <= 0:
            continue
        recent_n = max(1, int(round(budget * 0.75)))
        old_n = max(0, budget - recent_n)
        route_rows = _eligible_rows(connection, route, recent_n, oldest=False)
        used_route = {int(r["id"]) for r in route_rows}
        if old_n:
            for row in _eligible_rows(connection, route, old_n * 2 + 10, oldest=True):
                if int(row["id"]) in used_route:
                    continue
                route_rows.append(row); used_route.add(int(row["id"]))
                if len(route_rows) >= budget:
                    break
        selected.extend(route_rows[:budget])
    if len(selected) < target:
        missing = target - len(selected)
        used = {int(r["id"]) for r in selected}
        for route in ROUTES:
            extra = _eligible_rows(connection, route, target)
            for row in extra:
                if int(row["id"]) in used:
                    continue
                selected.append(row); used.add(int(row["id"])); missing -= 1
                if missing <= 0: break
            if missing <= 0: break
    route_rank = {"MUST_ANALYZE": 0, "ANALYZE": 1, "HOLD": 2}
    selected.sort(key=lambda r: (route_rank.get(str(r.get("route")), 9), r["ts"], int(r["id"])))
    return selected[:target]


def _story_index(connection: db.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT st.id,st.story_key,st.canonical_document_id,st.title,st.summary,
               st.first_seen_at,st.last_seen_at,
               ga.main_claim AS canonical_claim
          FROM grok_stories_v1 st
     LEFT JOIN grok_document_analysis_v1 ga
            ON ga.source_document_id=st.canonical_document_id
         WHERE st.status='active'
         ORDER BY st.last_seen_at DESC NULLS LAST,st.id DESC
         LIMIT 400
        """
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["tokens"] = _tokens(" ".join([
            str(item.get("story_key") or ""), str(item.get("title") or ""),
            str(item.get("summary") or ""), str(item.get("canonical_claim") or ""),
        ]))
        out.append(item)
    return out


def _story_matches(doc_text: str, stories: list[dict[str, Any]], limit: int = 4) -> list[dict[str, Any]]:
    tokens = _tokens(doc_text)
    ranked = []
    for story in stories:
        score = _dice(tokens, story["tokens"])
        if score < 0.15:
            continue
        ranked.append((score, story))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    out = []
    for score, story in ranked[:limit]:
        out.append({
            "story_key": story["story_key"],
            "canonical_document_id": story["canonical_document_id"],
            "title": story["title"],
            "summary": story["summary"],
            "last_seen_at": story["last_seen_at"],
            "heuristic_score": round(score, 4),
        })
    return out


def _cluster_context(connection: db.Connection, cluster_id: int | None) -> dict[str, Any]:
    if not cluster_id:
        return {}
    cluster = connection.execute(
        "SELECT * FROM primary_info_clusters_v1 WHERE id=?",
        (int(cluster_id),),
    ).fetchone()
    if not cluster:
        return {}
    members = connection.execute(
        """
        SELECT m.source_document_id,m.match_type,m.similarity,m.propagation_role,
               d.source_title,d.source_url,d.published_at
          FROM primary_info_cluster_members_v1 m
          JOIN source_documents d ON d.id=m.source_document_id
         WHERE m.cluster_id=?
         ORDER BY COALESCE(NULLIF(d.published_at,'')::timestamptz,
                           NULLIF(d.created_at,'')::timestamptz),d.id
         LIMIT 12
        """,
        (int(cluster_id),),
    ).fetchall()
    c = dict(cluster)
    return {
        "cluster_id": int(c["id"]),
        "canonical_document_id": c.get("canonical_document_id"),
        "spread_count": int(c.get("spread_count") or 0),
        "unique_sources": int(c.get("unique_sources") or 0),
        "unique_domains": int(c.get("unique_domains") or 0),
        "unique_telegram_channels": int(c.get("unique_telegram_channels") or 0),
        "cross_platform": bool(c.get("cross_platform")),
        "first_seen_at": c.get("first_seen_at"),
        "last_seen_at": c.get("last_seen_at"),
        "representative_sources": c.get("representative_sources") or [],
        "members_sample": [dict(r) for r in members],
    }


def _document_context(connection: db.Connection, selected: dict[str, Any], stories: list[dict[str, Any]]) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT d.*,COALESCE(s.groups,'[]'::jsonb) source_groups,
               COALESCE(s.audiences,'[]'::jsonb) source_audiences,
               COALESCE(s.country_spaces,'[]'::jsonb) source_country_spaces
          FROM source_documents d
     LEFT JOIN sources s ON s.id=d.source_id
         WHERE d.id=?
        """,
        (int(selected["id"]),),
    ).fetchone()
    if not row:
        raise RuntimeError(f"source document disappeared: {selected['id']}")
    d = dict(row)
    text = _text(d)
    basis = "full_text" if str(d.get("full_text") or "").strip() else (
        "source_text" if str(d.get("source_text") or "").strip() else "feed_text"
    )
    story_text = " ".join([str(d.get("source_title") or ""), text[:5000]])
    return {
        "source_document_id": int(d["id"]),
        "route": selected["route"],
        "source_kind": d.get("source_kind"),
        "source": d.get("source_title"),
        "url": d.get("source_url"),
        "published_at": d.get("published_at"),
        "created_at": d.get("created_at"),
        "content_status": d.get("content_status"),
        "source_groups": d.get("source_groups") or [],
        "source_audiences": d.get("source_audiences") or [],
        "source_country_spaces": d.get("source_country_spaces") or [],
        "input_basis": basis,
        "text": text,
        "prefilter_hypothesis": {
            "domain_score": selected.get("domain_score"),
            "influence_score": selected.get("influence_score"),
            "spread_score": selected.get("spread_score"),
            "actor_hits": selected.get("actor_hits") or [],
            "narrative_hits": selected.get("narrative_hits") or [],
            "hashtag_hits": selected.get("hashtag_hits") or [],
            "reason_codes": selected.get("reason_codes") or [],
        },
        "deterministic_cluster": _cluster_context(connection, selected.get("cluster_id")),
        "story_candidates": _story_matches(story_text, stories),
    }


def _adaptive_batch_plan(items: list[dict[str, Any]], requested: int = 0) -> tuple[int, int, list[list[dict[str, Any]]]]:
    if not items:
        return 0, 320000, []
    lengths = [len(str(item.get("text") or "")) for item in items]
    avg_chars = int(sum(lengths) / max(1, len(lengths)))
    if requested > 0:
        max_items = max(20, min(int(requested), 250))
    elif avg_chars <= 1600:
        max_items = 250
    elif avg_chars <= 2800:
        max_items = 220
    elif avg_chars <= 4500:
        max_items = 180
    elif avg_chars <= 6000:
        max_items = 150
    else:
        max_items = 120
    char_budget = 320000
    total_chars = sum(lengths)
    k = max(1, math.ceil(len(items) / max_items), math.ceil(total_chars / char_budget))
    indexed = list(enumerate(items))
    indexed.sort(key=lambda pair: len(str(pair[1].get("text") or "")), reverse=True)
    while k <= len(items):
        bins=[[] for _ in range(k)]; chars=[0 for _ in range(k)]
        ok=True
        for idx,item in indexed:
            size=len(str(item.get("text") or ""))
            choices=[i for i in range(k) if len(bins[i]) < max_items and chars[i] + size <= char_budget]
            if not choices:
                ok=False; break
            i=min(choices,key=lambda j:(chars[j],len(bins[j])))
            bins[i].append((idx,item)); chars[i]+=size
        if ok:
            batches=[]
            for b in bins:
                b.sort(key=lambda pair: pair[0])
                batches.append([item for _,item in b])
            return max_items,char_budget,batches
        k += 1
    return max_items,char_budget,[items]

def prepare_run(target: int = 250, batch_size: int = 0) -> dict[str, Any]:
    target = max(1, min(int(target), 500))
    batch_size = max(0, min(int(batch_size), 250))
    hold_sync = hold_v2.sync_now(SCOPE_START)
    propagation_sync = story_propagation.sync_now()
    connection = db.connect(readonly=True)
    try:
        counts_before = backlog_counts(connection)
        selected = select_backlog(connection, target)
        stories = _story_index(connection)
        items = [_document_context(connection, row, stories) for row in selected]
    finally:
        connection.close()

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    run_dir = ROOT / "runtime" / "grok_local_v3" / run_id
    batches_dir = run_dir / "batches"
    results_dir = run_dir / "results"
    batches_dir.mkdir(parents=True, exist_ok=False)
    results_dir.mkdir(parents=True, exist_ok=True)

    max_batch_items, char_budget, chunks = _adaptive_batch_plan(items, batch_size)
    batch_paths = []
    for index, chunk in enumerate(chunks, 1):
        batch_id = f"{run_id}_b{index:02d}"
        result_path = results_dir / f"{batch_id}.result.json"
        payload = {
            "batch_id": batch_id,
            "analyzer_version": VERSION,
            "run_id": run_id,
            "batch_index": index,
            "result_path": str(result_path),
            "system_prompt_path": str(ROOT / "grok" / "local_v3" / "SYSTEM_PROMPT_V3.md"),
            "schema_path": str(ROOT / "grok" / "local_v3" / "OUTPUT_SCHEMA_V3.json"),
            "item_count": len(chunk),
            "items": chunk,
        }
        batch_path = batches_dir / f"{batch_id}.json"
        batch_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        batch_paths.append(str(batch_path))

    manifest = {
        "run_id": run_id,
        "analyzer_version": VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "selected": len(items),
        "batch_size_requested": batch_size,
        "max_batch_items": max_batch_items,
        "max_batch_chars": char_budget,
        "average_text_chars": round(sum(len(str(i.get("text") or "")) for i in items) / max(1, len(items))),
        "batch_count": len(batch_paths),
        "batch_item_counts": [len(chunk) for chunk in chunks],
        "route_counts": dict(Counter(item["route"] for item in items)),
        "backlog_before": counts_before,
        "hold_v2": hold_sync,
        "hold_v2_version": hold_v2.VERSION,
        "primary_exact_propagation": propagation_sync,
        "first_published_at": items[0]["published_at"] if items else None,
        "last_published_at": items[-1]["published_at"] if items else None,
        "batch_paths": batch_paths,
        "results_dir": str(results_dir),
        "validation_command": str(ROOT / "scripts" / "grok_local_v3_validate.py"),
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    start_path = run_dir / "GROK_START.txt"
    start_path.write_text(_start_instructions(manifest_path, batch_paths), encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    manifest["start_path"] = str(start_path)
    return manifest


def _start_instructions(manifest_path: Path, batch_paths: list[str]) -> str:
    lines = [
        "Працюй через Local Execution / host shell. MCP не використовуй.",
        f"Корінь проєкту: {ROOT}",
        f"Маніфест запуску: {manifest_path}",
        f"Системний контракт: {ROOT / 'grok' / 'local_v3' / 'SYSTEM_PROMPT_V3.md'}",
        f"JSON schema: {ROOT / 'grok' / 'local_v3' / 'OUTPUT_SCHEMA_V3.json'}",
        "",
        "Якщо batch_count=1 — опрацюй усі items цього batch ОДНИМ branch / ОДНИМ context / ОДНИМ проходом, без субагентів і дроблення.",
        "Якщо batch_count>1 — опрацьовуй batch-файли послідовно, один batch = один context.",
        "Для кожного batch:",
        "1. Прочитай SYSTEM_PROMPT_V3.md і конкретний batch JSON.",
        "2. Проаналізуй рівно всі items цього batch.",
        "3. Запиши один JSON-об'єкт ТІЛЬКИ у result_path з batch-файлу. Не зберігай result на Desktop/Downloads.",
        "4. Запусти локальний validator для цього batch/result.",
        "5. Якщо validator не пройдено — виправ result і повтори validation; не переходь далі.",
        "6. Після успішної validation переходь до наступного batch.",
        "",
        "Не змінюй PostgreSQL, input batch-файли, код проєкту або старі grok_* таблиці.",
        f"Обмінні пакети лежать у: {ROOT / 'grok' / 'exchange' / 'to_grok'}. Повернені ZIP, якщо вони потрібні, клади у: {ROOT / 'grok' / 'exchange' / 'from_grok'}.",
        "Після всіх batch поверни коротко: batch_count, processed, KEEP/CONTEXT/REVIEW/DROP та шлях до run directory.",
        "",
        "Batch-файли:",
    ]
    lines.extend(f"- {path}" for path in batch_paths)
    lines.extend([
        "",
        "Validator command template:",
        f'& "{ROOT}\\.venv\\Scripts\\python.exe" "{ROOT}\\scripts\\grok_local_v3_validate.py" "<batch_path>" "<result_path>"',
    ])
    return "\n".join(lines) + "\n"
