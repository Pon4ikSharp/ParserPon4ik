"""Scoped Grok Analysis v1 tools.

All writes are restricted to grok_*_v1 tables.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app import db

PRIORITIES = {"low", "medium", "high"}
SPEAKER_ROLES = {
    "speaker", "quoted", "mentioned", "addressee",
    "future_speaker", "editorial", "none",
}
SOURCE_ROLES = {
    "originator", "official", "relay",
    "commentary", "aggregator", "unclear",
}
NOVELTIES = {"new", "development", "duplicate"}
STORY_RELATIONS = {"canonical", "duplicate", "development"}


def _bounded(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _clean_list(values: list[str] | None, max_items: int = 24) -> list[str]:
    out: list[str] = []
    for value in values or []:
        text = str(value).strip()
        if text and text not in out:
            out.append(text[:240])
        if len(out) >= max_items:
            break
    return out


def _json(values: Any) -> str:
    return json.dumps(values, ensure_ascii=False)


def _assert_pending(c: db.Connection, document_id: int) -> dict:
    row = c.execute(
        """
        SELECT q.source_document_id AS id, q.status, d.source_title,
               d.published_at, d.created_at
          FROM primary_info_queue_v1 q
          JOIN source_documents d ON d.id=q.source_document_id
         WHERE q.source_document_id=? AND q.status='pending'
        """,
        (int(document_id),),
    ).fetchone()
    if not row:
        raise ValueError(f"document {document_id} is not a pending candidate")
    return dict(row)


def register_analysis_tools(mcp) -> None:
    @mcp.tool()
    def get_analysis_protocol() -> dict:
        """Return the current Grok Analysis v1 methodology and workflow."""
        return {
            "version": "grok-analysis-v1-20260922",
            "scope": "information influence analysis; not generic news relevance",
            "rules": [
                "Treat automatic route, scores, actor hits and narratives only as hypotheses.",
                "Watchlist presence alone never makes a document important.",
                "Identify the actual speaker; distinguish quoted, mentioned, addressee and future_speaker roles.",
                "Do not infer hidden-truth/conspiracy from phrases like 'на самом деле' without concealment/deception markers.",
                "Pravda-family and similar outlets are normally relays/amplifiers, not canonical originators.",
                "Separate new story, development with added value, and pure duplicate.",
                "Noise may be relevant to news but still irrelevant to information-influence monitoring.",
                "Likely objectives are analytical inferences, not proven author intent.",
            ],
            "workflow": [
                "Read fresh items with get_analysis_candidates.",
                "Save every judged item with save_document_analysis.",
                "For keep_for_analysis=true, inspect recent stories with get_story_candidates.",
                "Create a story only when no existing story matches.",
                "Attach duplicate/development items to the matching story.",
            ],
        }

    @mcp.tool()
    def get_analysis_candidates(limit: int = 5, max_chars: int = 3500) -> dict:
        """Get newest pending documents not yet saved in Grok Analysis v1."""
        limit = _bounded(limit, 1, 10)
        max_chars = _bounded(max_chars, 500, 5000)
        c = db.connect(readonly=True)
        try:
            rows = c.execute(
                """
                SELECT q.source_document_id AS id, q.cluster_id,
                       d.source_kind, d.source_title AS source, d.source_url AS url,
                       d.published_at, d.created_at,
                       COALESCE(s.groups,'[]'::jsonb) AS source_groups,
                       COALESCE(s.audiences,'[]'::jsonb) AS audiences,
                       COALESCE(s.country_spaces,'[]'::jsonb) AS country_spaces,
                       LEFT(COALESCE(NULLIF(d.full_text,''),d.source_text,''), ?) AS text
                  FROM primary_info_queue_v1 q
                  JOIN source_documents d ON d.id=q.source_document_id
             LEFT JOIN sources s ON s.id=d.source_id
             LEFT JOIN grok_document_analysis_v1 ga ON ga.source_document_id=q.source_document_id
                 WHERE q.status='pending' AND ga.source_document_id IS NULL
                 ORDER BY COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                   NULLIF(d.created_at,'')::timestamptz) DESC NULLS LAST,
                          d.id DESC
                 LIMIT ?
                """,
                (max_chars, limit),
            ).fetchall()
            return {"count": len(rows), "items": [dict(r) for r in rows]}
        finally:
            c.close()

    @mcp.tool()
    def get_saved_analysis(document_id: int) -> dict:
        """Return the saved Grok Analysis v1 row for one document."""
        c = db.connect(readonly=True)
        try:
            row = c.execute(
                "SELECT * FROM grok_document_analysis_v1 WHERE source_document_id=?",
                (int(document_id),),
            ).fetchone()
            return {
                "found": bool(row),
                "analysis": dict(row) if row else None,
            }
        finally:
            c.close()

    @mcp.tool()
    def get_story_candidates(limit: int = 10, hours: int = 72) -> dict:
        """Return recently active Grok story records for semantic matching."""
        limit = _bounded(limit, 1, 20)
        hours = _bounded(hours, 1, 168)
        c = db.connect(readonly=True)
        try:
            rows = c.execute(
                """
                SELECT st.id, st.story_key, st.canonical_document_id,
                       st.title, st.summary, st.status,
                       st.narrative_keys, st.target_audiences, st.likely_objectives,
                       st.first_seen_at, st.last_seen_at,
                       count(sm.source_document_id)::int AS document_count,
                       count(*) FILTER (WHERE sm.relation='development')::int AS developments
                  FROM grok_stories_v1 st
             LEFT JOIN grok_story_members_v1 sm ON sm.story_id=st.id
                 WHERE st.status='active'
                   AND (st.last_seen_at IS NULL OR st.last_seen_at >= now() - (? * interval '1 hour'))
                 GROUP BY st.id
                 ORDER BY st.last_seen_at DESC NULLS LAST, st.id DESC
                 LIMIT ?
                """,
                (hours, limit),
            ).fetchall()
            return {"count": len(rows), "stories": [dict(r) for r in rows]}
        finally:
            c.close()

    @mcp.tool()
    def save_document_analysis(
        document_id: int,
        relevant: bool,
        keep_for_analysis: bool,
        priority: str,
        speaker: str = "",
        speaker_role: str = "none",
        mentioned_actors: list[str] | None = None,
        main_claim: str = "",
        narrative_keys: list[str] | None = None,
        narrative_labels: list[str] | None = None,
        target_audiences: list[str] | None = None,
        influence_methods: list[str] | None = None,
        likely_objectives: list[str] | None = None,
        source_role: str = "unclear",
        novelty: str = "new",
        story_key: str = "",
        duplicate_of_document_id: int | None = None,
        added_value: str = "",
        confidence: int = 0,
        reason: str = "",
        model: str = "grok-bot",
        prompt_version: str = "grok-analysis-v1",
        run_id: str = "",
    ) -> dict:
        """Save or update one normalized Grok document analysis."""
        priority = str(priority).strip().lower()
        speaker_role = str(speaker_role).strip().lower()
        source_role = str(source_role).strip().lower()
        novelty = str(novelty).strip().lower()
        confidence = _bounded(confidence, 0, 100)
        if priority not in PRIORITIES:
            raise ValueError("priority must be low, medium, or high")
        if speaker_role not in SPEAKER_ROLES:
            raise ValueError("invalid speaker_role")
        if source_role not in SOURCE_ROLES:
            raise ValueError("invalid source_role")
        if novelty not in NOVELTIES:
            raise ValueError("invalid novelty")
        if not relevant and keep_for_analysis:
            raise ValueError("noise cannot be keep_for_analysis=true")
        if novelty == "duplicate" and duplicate_of_document_id is None:
            raise ValueError("duplicate requires duplicate_of_document_id")
        if duplicate_of_document_id is not None and int(duplicate_of_document_id) == int(document_id):
            raise ValueError("document cannot duplicate itself")

        keys = _clean_list(narrative_keys)
        labels = _clean_list(narrative_labels)
        narratives = [
            {"key": key, "label": labels[i] if i < len(labels) else key}
            for i, key in enumerate(keys)
        ]
        payload = {
            "relevant": bool(relevant),
            "keep_for_analysis": bool(keep_for_analysis),
            "priority": priority,
            "speaker": str(speaker).strip()[:300],
            "speaker_role": speaker_role,
            "mentioned_actors": _clean_list(mentioned_actors),
            "main_claim": str(main_claim).strip()[:4000],
            "narratives": narratives,
            "target_audiences": _clean_list(target_audiences),
            "influence_methods": _clean_list(influence_methods),
            "likely_objectives": _clean_list(likely_objectives),
            "source_role": source_role,
            "novelty": novelty,
            "story_key": str(story_key).strip()[:180],
            "duplicate_of_document_id": (
                int(duplicate_of_document_id)
                if duplicate_of_document_id is not None else None
            ),
            "added_value": str(added_value).strip()[:4000],
            "confidence": confidence,
            "reason": str(reason).strip()[:5000],
            "model": str(model).strip()[:160] or "grok-bot",
            "prompt_version": str(prompt_version).strip()[:160] or "grok-analysis-v1",
            "run_id": str(run_id).strip()[:160],
        }

        c = db.connect(readonly=False)
        try:
            _assert_pending(c, document_id)
            if payload["duplicate_of_document_id"] is not None:
                other = c.execute(
                    "SELECT id FROM source_documents WHERE id=?",
                    (payload["duplicate_of_document_id"],),
                ).fetchone()
                if not other:
                    raise ValueError("duplicate_of_document_id does not exist")
            c.execute(
                """
                INSERT INTO grok_document_analysis_v1(
                  source_document_id,relevant,keep_for_analysis,priority,
                  speaker,speaker_role,mentioned_actors,main_claim,narratives,
                  target_audiences,influence_methods,likely_objectives,
                  source_role,novelty,story_key,duplicate_of_document_id,
                  added_value,confidence,reason,model,prompt_version,run_id,updated_at
                )
                VALUES(?,?,?,?,?,?,?::jsonb,?,?::jsonb,?::jsonb,?::jsonb,?::jsonb,
                       ?,?,?,?, ?,?,?,?,?,?,now())
                ON CONFLICT(source_document_id) DO UPDATE SET
                  relevant=excluded.relevant,
                  keep_for_analysis=excluded.keep_for_analysis,
                  priority=excluded.priority,
                  speaker=excluded.speaker,
                  speaker_role=excluded.speaker_role,
                  mentioned_actors=excluded.mentioned_actors,
                  main_claim=excluded.main_claim,
                  narratives=excluded.narratives,
                  target_audiences=excluded.target_audiences,
                  influence_methods=excluded.influence_methods,
                  likely_objectives=excluded.likely_objectives,
                  source_role=excluded.source_role,
                  novelty=excluded.novelty,
                  story_key=excluded.story_key,
                  duplicate_of_document_id=excluded.duplicate_of_document_id,
                  added_value=excluded.added_value,
                  confidence=excluded.confidence,
                  reason=excluded.reason,
                  model=excluded.model,
                  prompt_version=excluded.prompt_version,
                  run_id=excluded.run_id,
                  updated_at=now()
                """,
                (
                    int(document_id),
                    payload["relevant"],
                    payload["keep_for_analysis"],
                    payload["priority"],
                    payload["speaker"],
                    payload["speaker_role"],
                    _json(payload["mentioned_actors"]),
                    payload["main_claim"],
                    _json(payload["narratives"]),
                    _json(payload["target_audiences"]),
                    _json(payload["influence_methods"]),
                    _json(payload["likely_objectives"]),
                    payload["source_role"],
                    payload["novelty"],
                    payload["story_key"],
                    payload["duplicate_of_document_id"],
                    payload["added_value"],
                    payload["confidence"],
                    payload["reason"],
                    payload["model"],
                    payload["prompt_version"],
                    payload["run_id"],
                ),
            )
            c.execute(
                """
                INSERT INTO grok_analysis_audit_v1(source_document_id,action,payload)
                VALUES(?, 'save_document_analysis', ?::jsonb)
                """,
                (int(document_id), _json(payload)),
            )
            c.commit()
            return {
                "ok": True,
                "document_id": int(document_id),
                "stored": payload,
            }
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    @mcp.tool()
    def create_story(
        story_key: str,
        canonical_document_id: int,
        title: str,
        summary: str = "",
        narrative_keys: list[str] | None = None,
        target_audiences: list[str] | None = None,
        likely_objectives: list[str] | None = None,
    ) -> dict:
        """Create one story from an analyzed document. Idempotent by story_key."""
        story_key = str(story_key).strip()[:180]
        if not story_key:
            raise ValueError("story_key is required")
        c = db.connect(readonly=False)
        try:
            analysis = c.execute(
                """
                SELECT source_document_id,keep_for_analysis
                  FROM grok_document_analysis_v1
                 WHERE source_document_id=?
                """,
                (int(canonical_document_id),),
            ).fetchone()
            if not analysis or not analysis["keep_for_analysis"]:
                raise ValueError("canonical document must have keep_for_analysis=true")
            doc = c.execute(
                """
                SELECT COALESCE(NULLIF(published_at,'')::timestamptz,
                                NULLIF(created_at,'')::timestamptz) AS ts
                  FROM source_documents WHERE id=?
                """,
                (int(canonical_document_id),),
            ).fetchone()
            existing = c.execute(
                "SELECT * FROM grok_stories_v1 WHERE story_key=?",
                (story_key,),
            ).fetchone()
            if existing and int(existing["canonical_document_id"] or 0) != int(canonical_document_id):
                raise ValueError("story_key already exists with another canonical document")
            if not existing:
                c.execute(
                    """
                    INSERT INTO grok_stories_v1(
                      story_key,canonical_document_id,title,summary,
                      narrative_keys,target_audiences,likely_objectives,
                      first_seen_at,last_seen_at
                    )
                    VALUES(?,?,?,?,?::jsonb,?::jsonb,?::jsonb,?,?)
                    """,
                    (
                        story_key,
                        int(canonical_document_id),
                        str(title).strip()[:500],
                        str(summary).strip()[:5000],
                        _json(_clean_list(narrative_keys)),
                        _json(_clean_list(target_audiences)),
                        _json(_clean_list(likely_objectives)),
                        doc["ts"] if doc else None,
                        doc["ts"] if doc else None,
                    ),
                )
            story = c.execute(
                "SELECT * FROM grok_stories_v1 WHERE story_key=?",
                (story_key,),
            ).fetchone()
            c.execute(
                """
                INSERT INTO grok_story_members_v1(
                  story_id,source_document_id,relation,added_value
                )
                VALUES(?,?,'canonical','')
                ON CONFLICT(source_document_id) DO NOTHING
                """,
                (int(story["id"]), int(canonical_document_id)),
            )
            c.execute(
                """
                UPDATE grok_document_analysis_v1
                   SET story_key=?, novelty='new', duplicate_of_document_id=NULL,
                       updated_at=now()
                 WHERE source_document_id=?
                """,
                (story_key, int(canonical_document_id)),
            )
            audit = {
                "story_key": story_key,
                "canonical_document_id": int(canonical_document_id),
                "story_id": int(story["id"]),
            }
            c.execute(
                """
                INSERT INTO grok_analysis_audit_v1(source_document_id,action,payload)
                VALUES(?, 'create_story', ?::jsonb)
                """,
                (int(canonical_document_id), _json(audit)),
            )
            c.commit()
            return {"ok": True, "story": dict(story)}
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    @mcp.tool()
    def attach_document_to_story(
        story_key: str,
        document_id: int,
        relation: str,
        added_value: str = "",
    ) -> dict:
        """Attach an analyzed document to an existing story."""
        story_key = str(story_key).strip()[:180]
        relation = str(relation).strip().lower()
        if relation not in STORY_RELATIONS:
            raise ValueError("invalid relation")
        c = db.connect(readonly=False)
        try:
            story = c.execute(
                "SELECT * FROM grok_stories_v1 WHERE story_key=?",
                (story_key,),
            ).fetchone()
            if not story:
                raise ValueError("story_key does not exist")
            analysis = c.execute(
                "SELECT * FROM grok_document_analysis_v1 WHERE source_document_id=?",
                (int(document_id),),
            ).fetchone()
            if not analysis:
                raise ValueError("document must be saved in Grok Analysis v1 first")
            if not analysis["keep_for_analysis"]:
                raise ValueError("only keep_for_analysis=true documents may join a story")
            existing = c.execute(
                """
                SELECT sm.story_id, st.story_key
                  FROM grok_story_members_v1 sm
                  JOIN grok_stories_v1 st ON st.id=sm.story_id
                 WHERE sm.source_document_id=?
                """,
                (int(document_id),),
            ).fetchone()
            if existing and int(existing["story_id"]) != int(story["id"]):
                raise ValueError(
                    f"document already belongs to story {existing['story_key']}"
                )
            if relation == "canonical" and int(story["canonical_document_id"] or 0) != int(document_id):
                raise ValueError("canonical relation is reserved for the story canonical document")
            c.execute(
                """
                INSERT INTO grok_story_members_v1(
                  story_id,source_document_id,relation,added_value
                )
                VALUES(?,?,?,?)
                ON CONFLICT(source_document_id) DO UPDATE SET
                  relation=excluded.relation,
                  added_value=excluded.added_value
                """,
                (
                    int(story["id"]),
                    int(document_id),
                    relation,
                    str(added_value).strip()[:4000],
                ),
            )
            duplicate_of = (
                int(story["canonical_document_id"])
                if relation in {"duplicate", "development"} and story["canonical_document_id"]
                else None
            )
            novelty = "new" if relation == "canonical" else relation
            c.execute(
                """
                UPDATE grok_document_analysis_v1
                   SET story_key=?, novelty=?, duplicate_of_document_id=?,
                       added_value=?, updated_at=now()
                 WHERE source_document_id=?
                """,
                (
                    story_key,
                    novelty,
                    duplicate_of,
                    str(added_value).strip()[:4000],
                    int(document_id),
                ),
            )
            c.execute(
                """
                UPDATE grok_stories_v1 st
                   SET first_seen_at=x.first_seen,
                       last_seen_at=x.last_seen,
                       updated_at=now()
                  FROM (
                    SELECT min(COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                        NULLIF(d.created_at,'')::timestamptz)) AS first_seen,
                           max(COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                        NULLIF(d.created_at,'')::timestamptz)) AS last_seen
                      FROM grok_story_members_v1 sm
                      JOIN source_documents d ON d.id=sm.source_document_id
                     WHERE sm.story_id=?
                  ) x
                 WHERE st.id=?
                """,
                (int(story["id"]), int(story["id"])),
            )
            audit = {
                "story_key": story_key,
                "story_id": int(story["id"]),
                "relation": relation,
                "added_value": str(added_value).strip()[:4000],
            }
            c.execute(
                """
                INSERT INTO grok_analysis_audit_v1(source_document_id,action,payload)
                VALUES(?, 'attach_document_to_story', ?::jsonb)
                """,
                (int(document_id), _json(audit)),
            )
            c.commit()
            return {
                "ok": True,
                "document_id": int(document_id),
                "story_key": story_key,
                "relation": relation,
            }
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()
