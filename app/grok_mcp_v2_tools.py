"""Grok Analysis v2 tools: resumable batches and safer story handling."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from app import db

PRIORITIES={"low","medium","high"}
SPEAKER_ROLES={"speaker","quoted","mentioned","addressee","future_speaker","editorial","none"}
SOURCE_ROLES={"originator","official","relay","commentary","aggregator","unclear"}


def _s(value: Any, limit: int = 4000) -> str:
    return "" if value is None else str(value).strip()[:limit]


def _b(value: Any) -> bool:
    if isinstance(value,bool):
        return value
    if isinstance(value,(int,float)):
        return bool(value)
    text=str(value).strip().lower()
    if text in {"true","1","yes","y"}:
        return True
    if text in {"false","0","no","n",""}:
        return False
    raise ValueError(f"invalid boolean: {value!r}")


def _i(value: Any, low: int = 0, high: int = 100) -> int:
    return max(low,min(high,int(value or 0)))


def _lst(value: Any, max_items: int = 24) -> list[str]:
    if value is None:
        return []
    if isinstance(value,str):
        value=[value]
    if not isinstance(value,(list,tuple,set)):
        value=[value]
    out=[]
    for item in value:
        text=_s(item,240)
        if text and text not in out:
            out.append(text)
        if len(out)>=max_items:
            break
    return out


def _j(value: Any) -> str:
    return json.dumps(value,ensure_ascii=False)


def _tokens(text: str) -> set[str]:
    normalized="".join(ch.lower() if ch.isalnum() else " " for ch in (text or ""))
    stop={
        "the","and","for","with","that","this","from","have","has",
        "что","это","как","для","при","или","его","она","они","также",
        "про","який","яка","які","для","цей","ця","було","була","були",
    }
    return {x for x in normalized.split() if len(x)>=3 and not x.isdigit() and x not in stop}


def _dice(left: set[str], right: set[str]) -> tuple[float,list[str]]:
    if not left or not right:
        return 0.0,[]
    common=left & right
    score=(2.0*len(common))/(len(left)+len(right))
    return score,sorted(common)[:20]


def register_v2_tools(mcp) -> None:
    @mcp.tool()
    def health() -> dict:
        """Health and Analysis v2 counters."""
        c=db.connect(readonly=True)
        try:
            q=c.execute("SELECT status,count(*) n FROM primary_info_queue_v1 GROUP BY status").fetchall()
            a=c.execute(
                "SELECT count(*)::int analyzed,"
                "count(*) FILTER(WHERE relevant)::int relevant,"
                "count(*) FILTER(WHERE keep_for_analysis)::int kept "
                "FROM grok_document_analysis_v1"
            ).fetchone()
            s=c.execute("SELECT count(*)::int n FROM grok_stories_v1").fetchone()
            runs=c.execute(
                "SELECT count(*) FILTER(WHERE status='open')::int open_runs,"
                "count(*) FILTER(WHERE status='completed')::int completed_runs "
                "FROM grok_analysis_runs_v1"
            ).fetchone()
            review=c.execute(
                "SELECT count(*)::int n FROM grok_story_review_v1 WHERE status='pending'"
            ).fetchone()
            semantic=c.execute(
                """
                SELECT
                  (SELECT count(*)::int FROM grok_narratives_v1 WHERE status='active') AS narratives,
                  (SELECT count(*)::int FROM grok_theses_v1 WHERE status='active') AS theses,
                  (SELECT count(*)::int FROM grok_story_theses_v1) AS story_thesis_links
                """
            ).fetchone()
            backlog=c.execute(
                """
                SELECT p.route,count(*)::int AS n
                  FROM info_prefilter_results_v1 p
                  JOIN source_documents d ON d.id=p.source_document_id
             LEFT JOIN grok_document_analysis_v1 ga ON ga.source_document_id=d.id
                 WHERE p.route IN ('MUST_ANALYZE','ANALYZE')
                   AND ga.source_document_id IS NULL
                   AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                NULLIF(d.created_at,'')::timestamptz)
                       >= (TIMESTAMP '2026-09-21 00:00:00' AT TIME ZONE 'Europe/Kyiv')
                   AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                NULLIF(d.created_at,'')::timestamptz)
                       <= now()+interval '5 minutes'
                 GROUP BY p.route
                """
            ).fetchall()
            return {
                "ok":True,
                "service":"ParserPon4ik Grok Analysis v2",
                "mode":"analysis_v2.1_semantic_memory",
                "queue":{str(r["status"]):int(r["n"]) for r in q},
                "analysis":dict(a),
                "stories":int(s["n"]),
                "semantic_memory":dict(semantic),
                "eligible_backlog_since_2026_09_21":{
                    str(r["route"]):int(r["n"]) for r in backlog
                },
                "runs":dict(runs),
                "story_review_pending":int(review["n"]),
            }
        finally:
            c.close()


    @mcp.tool()
    def get_protocol() -> dict:
        """Return the Analysis v2 rules and unattended workflow."""
        return {
            "version":"grok-processor-v3-batch100-20260922",
            "scope_routes":["MUST_ANALYZE","ANALYZE"],
            "workflow":[
                "Call start_or_resume_run first; never bypass the run for routine work.",
                "Only MUST_ANALYZE and ANALYZE items from the configured Kyiv cutoff are claimable.",
                "Read only documents from get_run_documents for that run_key.",
                "Save each judgment with save_analysis using one analysis object.",
                "For kept items eligible for story assignment call get_story_matches(document_id); it returns Story, Thesis and Narrative candidates.",
                "Use apply_story_decision action=create or attach; low-confidence review items need no story.",
                "For a new story or a legacy story without semantic memory, include decision.semantic with thesis_key, thesis_label, thesis, and one or more narratives.",
                "Reuse candidate Thesis/Narrative keys when semantically equivalent; use new stable keys only when genuinely new.",
                "Duplicates attached to a story with semantic memory inherit it automatically and do not need decision.semantic.",
                "Call finish_run; it completes only when story and semantic requirements are satisfied.",
            ],
            "rules":[
                "Watchlist hits and deterministic routes are hypotheses, not truth.",
                "Distinguish actual speaker from quoted, mentioned, addressee and future speaker.",
                "Relay/amplifier is not automatically canonical origin.",
                "Narrative is broader than story; story is a concrete event/thesis.",
                "New story keys should describe the story, not the speaker or outlet.",
                "development requires a non-empty added_value explaining the material change.",
                "Pure restatement, new headline, language or outlet is duplicate.",
                "If confidence is below 60 or text is insufficient, do not create a new story; send it to story review.",
                "Likely objectives are analytical communication-function inferences, not proven intent.",
            ],
        }


    @mcp.tool()
    def start_or_resume_run(
        batch_size: int = 10,
        worker: str = "grok-routine",
        since_kyiv: str = "2026-09-21T00:00:00",
    ) -> dict:
        """Resume this worker's open run, otherwise claim eligible MUST/ANALYZE items."""
        batch_size=_i(batch_size,1,20)
        worker=_s(worker,120) or "grok-routine"
        since_kyiv=_s(since_kyiv,40) or "2026-09-21T00:00:00"
        c=db.connect(readonly=False)
        try:
            cur=c.raw.cursor()
            cur.execute("BEGIN")
            existing=cur.execute(
                """
                SELECT run_key,status,requested_count,claimed_count,processed_count,created_at
                  FROM grok_analysis_runs_v1
                 WHERE worker=%s AND status='open'
                 ORDER BY created_at DESC LIMIT 1
                 FOR UPDATE
                """,
                (worker,),
            ).fetchone()
            if existing:
                cur.execute("COMMIT")
                return {"resumed":True,"run":dict(existing)}
            rows=cur.execute(
                """
                SELECT q.source_document_id
                  FROM primary_info_queue_v1 q
                  JOIN source_documents d ON d.id=q.source_document_id
                  JOIN info_prefilter_results_v1 p
                    ON p.source_document_id=q.source_document_id
             LEFT JOIN grok_document_analysis_v1 ga
                    ON ga.source_document_id=q.source_document_id
                 WHERE q.status='pending'
                   AND p.route IN ('MUST_ANALYZE','ANALYZE')
                   AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                NULLIF(d.created_at,'')::timestamptz)
                       >= (%s::timestamp AT TIME ZONE 'Europe/Kyiv')
                   AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                NULLIF(d.created_at,'')::timestamptz)
                       <= now()+interval '5 minutes'
                   AND ga.source_document_id IS NULL
                   AND NOT EXISTS (
                     SELECT 1
                       FROM grok_analysis_run_items_v1 ri
                       JOIN grok_analysis_runs_v1 rr ON rr.run_key=ri.run_key
                      WHERE ri.source_document_id=q.source_document_id
                        AND rr.status='open'
                   )
                 ORDER BY COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                   NULLIF(d.created_at,'')::timestamptz) ASC NULLS LAST,
                          d.id ASC
                 LIMIT %s
                 FOR UPDATE OF q SKIP LOCKED
                """,
                (since_kyiv,batch_size),
            ).fetchall()
            if not rows:
                cur.execute("ROLLBACK")
                return {"resumed":False,"empty":True,"run":None}
            run_key=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"_"+uuid.uuid4().hex[:10]
            cur.execute(
                """
                INSERT INTO grok_analysis_runs_v1(
                  run_key,worker,status,requested_count,claimed_count,processed_count,meta
                )
                VALUES(%s,%s,'open',%s,%s,0,%s::jsonb)
                """,
                (
                    run_key,worker,batch_size,len(rows),
                    _j({
                        "routes":["MUST_ANALYZE","ANALYZE"],
                        "since_kyiv":since_kyiv,
                        "order":"oldest_first",
                    }),
                ),
            )
            for idx,row in enumerate(rows,1):
                cur.execute(
                    """
                    INSERT INTO grok_analysis_run_items_v1(
                      run_key,source_document_id,ordinal,status
                    )
                    VALUES(%s,%s,%s,'claimed')
                    """,
                    (run_key,int(row["source_document_id"]),idx),
                )
            cur.execute("COMMIT")
            return {
                "resumed":False,
                "empty":False,
                "run":{
                    "run_key":run_key,
                    "worker":worker,
                    "status":"open",
                    "requested_count":batch_size,
                    "claimed_count":len(rows),
                    "processed_count":0,
                },
                "document_ids":[int(r["source_document_id"]) for r in rows],
            }
        except Exception:
            c.raw.rollback()
            raise
        finally:
            c.close()


    @mcp.tool()
    def get_run_documents(run_key: str, max_chars: int = 5000) -> dict:
        """Read all claimed documents for one open run, including item status."""
        run_key=_s(run_key,160)
        max_chars=_i(max_chars,500,12000)
        c=db.connect(readonly=True)
        try:
            run=c.execute(
                "SELECT * FROM grok_analysis_runs_v1 WHERE run_key=?",
                (run_key,),
            ).fetchone()
            if not run:
                raise ValueError("unknown run_key")
            rows=c.execute(
                """
                SELECT ri.ordinal,ri.status AS run_status,ri.semantic_status,
                       ri.source_document_id AS id,p.route,
                       d.source_kind,d.source_title AS source,d.source_url AS url,
                       d.published_at,d.created_at,
                       COALESCE(s.groups,'[]'::jsonb) source_groups,
                       COALESCE(s.audiences,'[]'::jsonb) audiences,
                       COALESCE(s.country_spaces,'[]'::jsonb) country_spaces,
                       LEFT(COALESCE(NULLIF(d.full_text,''),d.source_text,''),?) AS text
                  FROM grok_analysis_run_items_v1 ri
                  JOIN source_documents d ON d.id=ri.source_document_id
                  JOIN info_prefilter_results_v1 p
                    ON p.source_document_id=ri.source_document_id
             LEFT JOIN sources s ON s.id=d.source_id
                 WHERE ri.run_key=?
                 ORDER BY ri.ordinal
                """,
                (max_chars,run_key),
            ).fetchall()
            return {"run":dict(run),"count":len(rows),"items":[dict(r) for r in rows]}
        finally:
            c.close()

    @mcp.tool()
    def get_document(document_id: int, max_chars: int = 12000) -> dict:
        """Read one source document for fuller context; never mutates."""
        max_chars=_i(max_chars,500,20000)
        c=db.connect(readonly=True)
        try:
            row=c.execute(
                """
                SELECT d.id,d.source_kind,d.source_title AS source,d.source_url AS url,
                       d.published_at,d.created_at,d.content_status,
                       COALESCE(s.groups,'[]'::jsonb) source_groups,
                       COALESCE(s.audiences,'[]'::jsonb) audiences,
                       COALESCE(s.country_spaces,'[]'::jsonb) country_spaces,
                       LEFT(COALESCE(NULLIF(d.full_text,''),d.source_text,''),?) AS text
                  FROM source_documents d
             LEFT JOIN sources s ON s.id=d.source_id
                 WHERE d.id=?
                """,
                (max_chars,int(document_id)),
            ).fetchone()
            return {"found":bool(row),"document":dict(row) if row else None}
        finally:
            c.close()


    @mcp.tool()
    def get_story_matches(document_id: int, limit: int = 8, hours: int = 168) -> dict:
        """Rank likely existing stories for this exact document; heuristic only."""
        limit=_i(limit,1,15)
        hours=_i(hours,1,720)
        c=db.connect(readonly=True)
        try:
            doc=c.execute(
                """
                SELECT d.id,d.source_title,
                       LEFT(COALESCE(NULLIF(d.full_text,''),d.source_text,''),6000) AS text,
                       ga.main_claim,ga.narratives
                  FROM source_documents d
             LEFT JOIN grok_document_analysis_v1 ga ON ga.source_document_id=d.id
                 WHERE d.id=?
                """,
                (int(document_id),),
            ).fetchone()
            if not doc:
                raise ValueError("unknown document_id")
            doc_text=" ".join([
                _s(doc["source_title"],500),
                _s(doc["main_claim"],4000),
                _s(doc["text"],6000),
            ])
            doc_tokens=_tokens(doc_text)
            doc_narr={
                _s(x.get("key"),180)
                for x in (doc["narratives"] or [])
                if isinstance(x,dict) and _s(x.get("key"),180)
            }
            stories=c.execute(
                """
                SELECT st.id,st.story_key,st.canonical_document_id,st.title,st.summary,
                       st.narrative_keys,st.target_audiences,st.likely_objectives,
                       st.first_seen_at,st.last_seen_at,
                       ga.main_claim AS canonical_claim,ga.narratives AS canonical_narratives,
                       EXISTS(
                         SELECT 1 FROM grok_story_theses_v1 gst
                          WHERE gst.story_id=st.id
                       ) AS semantic_ready,
                       (
                         SELECT gt.thesis_key
                           FROM grok_story_theses_v1 gst
                           JOIN grok_theses_v1 gt ON gt.id=gst.thesis_id
                          WHERE gst.story_id=st.id AND gst.relation='primary'
                          LIMIT 1
                       ) AS primary_thesis_key
                  FROM grok_stories_v1 st
             LEFT JOIN grok_document_analysis_v1 ga
                    ON ga.source_document_id=st.canonical_document_id
             LEFT JOIN grok_story_review_v1 rv
                    ON rv.source_document_id=st.canonical_document_id
                 WHERE st.status='active'
                   AND (rv.status IS NULL OR rv.status<>'pending')
                   AND (st.last_seen_at IS NULL OR
                        st.last_seen_at >= now() - (? * interval '1 hour'))
                 ORDER BY st.last_seen_at DESC NULLS LAST
                 LIMIT 300
                """,
                (hours,),
            ).fetchall()
            ranked=[]
            for st in stories:
                story_text=" ".join([
                    _s(st["story_key"],180),
                    _s(st["title"],500),
                    _s(st["summary"],5000),
                    _s(st["canonical_claim"],4000),
                ])
                token_score,common=_dice(doc_tokens,_tokens(story_text))
                story_narr=set(_lst(st["narrative_keys"]))
                if st["canonical_narratives"]:
                    story_narr.update(
                        _s(x.get("key"),180)
                        for x in st["canonical_narratives"]
                        if isinstance(x,dict) and _s(x.get("key"),180)
                    )
                narr_overlap=sorted(doc_narr & story_narr)
                score=min(1.0,token_score*0.75+(0.35 if narr_overlap else 0.0))
                ranked.append({
                    "story_id":int(st["id"]),
                    "story_key":st["story_key"],
                    "canonical_document_id":st["canonical_document_id"],
                    "title":st["title"],
                    "summary":st["summary"],
                    "heuristic_score":round(score,4),
                    "token_score":round(token_score,4),
                    "common_terms":common,
                    "narrative_overlap":narr_overlap,
                    "semantic_ready":bool(st["semantic_ready"]),
                    "primary_thesis_key":st["primary_thesis_key"],
                    "last_seen_at":st["last_seen_at"],
                })
            ranked.sort(key=lambda x:(x["heuristic_score"],x["last_seen_at"] or datetime.min.replace(tzinfo=timezone.utc)),reverse=True)

            thesis_candidates=[]
            for row in c.execute(
                """
                SELECT thesis_key,label,thesis,description,confidence,last_seen_at
                  FROM grok_theses_v1
                 WHERE status='active'
                 ORDER BY last_seen_at DESC NULLS LAST,id DESC
                 LIMIT 300
                """
            ).fetchall():
                score,common=_dice(doc_tokens,_tokens(" ".join([
                    _s(row["thesis_key"],180),_s(row["label"],500),
                    _s(row["thesis"],4000),_s(row["description"],3000),
                ])))
                thesis_candidates.append({
                    "thesis_key":row["thesis_key"],"label":row["label"],
                    "thesis":row["thesis"],"confidence":row["confidence"],
                    "heuristic_score":round(score,4),"common_terms":common,
                    "last_seen_at":row["last_seen_at"],
                })
            thesis_candidates.sort(
                key=lambda x:(x["heuristic_score"],x["last_seen_at"] or datetime.min.replace(tzinfo=timezone.utc)),
                reverse=True,
            )

            narrative_candidates=[]
            for row in c.execute(
                """
                SELECT narrative_key,label,description,confidence,last_seen_at
                  FROM grok_narratives_v1
                 WHERE status='active'
                 ORDER BY last_seen_at DESC NULLS LAST,id DESC
                 LIMIT 200
                """
            ).fetchall():
                score,common=_dice(doc_tokens,_tokens(" ".join([
                    _s(row["narrative_key"],180),_s(row["label"],500),
                    _s(row["description"],3000),
                ])))
                if row["narrative_key"] in doc_narr:
                    score=min(1.0,score+0.40)
                narrative_candidates.append({
                    "narrative_key":row["narrative_key"],"label":row["label"],
                    "description":row["description"],"confidence":row["confidence"],
                    "heuristic_score":round(score,4),"common_terms":common,
                    "last_seen_at":row["last_seen_at"],
                })
            narrative_candidates.sort(
                key=lambda x:(x["heuristic_score"],x["last_seen_at"] or datetime.min.replace(tzinfo=timezone.utc)),
                reverse=True,
            )
            return {
                "document_id":int(document_id),
                "candidate_count":min(limit,len(ranked)),
                "candidates":ranked[:limit],
                "thesis_candidates":thesis_candidates[:limit],
                "narrative_candidates":narrative_candidates[:limit],
                "note":"Story/Thesis/Narrative candidates are retrieval hints; Grok makes the semantic decision.",
            }
        finally:
            c.close()


    @mcp.tool()
    def save_analysis(run_key: str, document_id: int, analysis: dict[str,Any]) -> dict:
        """Save one normalized analysis using a tolerant object schema."""
        run_key=_s(run_key,160)
        if not isinstance(analysis,dict):
            raise ValueError("analysis must be an object")
        for key in ("relevant","keep_for_analysis","priority"):
            if key not in analysis:
                raise ValueError(f"analysis.{key} is required")

        relevant=_b(analysis.get("relevant"))
        keep=_b(analysis.get("keep_for_analysis"))
        priority=_s(analysis.get("priority"),20).lower()
        if priority not in PRIORITIES:
            raise ValueError("priority must be low, medium, or high")
        if not relevant and keep:
            raise ValueError("irrelevant document cannot be kept")

        confidence=_i(analysis.get("confidence",0),0,100)
        insufficient=_b(analysis.get("insufficient_text",False))
        speaker=_s(analysis.get("speaker"),300)
        speaker_role=_s(analysis.get("speaker_role","none"),40).lower() or "none"
        if speaker_role not in SPEAKER_ROLES:
            speaker_role="none"
        source_role=_s(analysis.get("source_role","unclear"),40).lower() or "unclear"
        if source_role not in SOURCE_ROLES:
            source_role="unclear"

        keys=_lst(analysis.get("narrative_keys"))
        labels=_lst(analysis.get("narrative_labels"))
        narratives=[
            {"key":key,"label":labels[i] if i<len(labels) else key}
            for i,key in enumerate(keys)
        ]
        payload={
            "relevant":relevant,
            "keep_for_analysis":keep,
            "priority":priority,
            "speaker":speaker,
            "speaker_role":speaker_role,
            "mentioned_actors":_lst(analysis.get("mentioned_actors")),
            "main_claim":_s(analysis.get("main_claim"),4000),
            "narratives":narratives,
            "target_audiences":_lst(analysis.get("target_audiences")),
            "influence_methods":_lst(analysis.get("influence_methods")),
            "likely_objectives":_lst(analysis.get("likely_objectives")),
            "source_role":source_role,
            "confidence":confidence,
            "reason":_s(analysis.get("reason"),5000),
            "model":_s(analysis.get("model"),160) or "grok-bot",
            "prompt_version":_s(analysis.get("prompt_version"),160) or "grok-analysis-v2",
            "insufficient_text":insufficient,
        }

        c=db.connect(readonly=False)
        try:
            cur=c.raw.cursor()
            cur.execute("BEGIN")
            run=cur.execute(
                "SELECT * FROM grok_analysis_runs_v1 WHERE run_key=%s FOR UPDATE",
                (run_key,),
            ).fetchone()
            if not run or run["status"]!="open":
                raise ValueError("run_key is not an open run")
            item=cur.execute(
                """
                SELECT * FROM grok_analysis_run_items_v1
                 WHERE run_key=%s AND source_document_id=%s
                 FOR UPDATE
                """,
                (run_key,int(document_id)),
            ).fetchone()
            if not item:
                raise ValueError("document is not claimed by this run")
            if item["status"]=="done":
                existing=cur.execute(
                    "SELECT * FROM grok_document_analysis_v1 WHERE source_document_id=%s",
                    (int(document_id),),
                ).fetchone()
                cur.execute("COMMIT")
                return {
                    "ok":True,
                    "idempotent":True,
                    "document_id":int(document_id),
                    "analysis":dict(existing) if existing else None,
                }

            cur.execute(
                """
                INSERT INTO grok_document_analysis_v1(
                  source_document_id,relevant,keep_for_analysis,priority,
                  speaker,speaker_role,mentioned_actors,main_claim,narratives,
                  target_audiences,influence_methods,likely_objectives,
                  source_role,novelty,story_key,duplicate_of_document_id,
                  added_value,confidence,reason,model,prompt_version,run_id,
                  raw_result,updated_at
                )
                VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,
                       %s::jsonb,%s::jsonb,%s::jsonb,%s,'new','',NULL,'',
                       %s,%s,%s,%s,%s,%s::jsonb,now())
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
                  novelty='new',story_key='',duplicate_of_document_id=NULL,added_value='',
                  confidence=excluded.confidence,reason=excluded.reason,
                  model=excluded.model,prompt_version=excluded.prompt_version,
                  run_id=excluded.run_id,raw_result=excluded.raw_result,updated_at=now()
                """,
                (
                    int(document_id),
                    relevant,
                    keep,
                    priority,
                    speaker,
                    speaker_role,
                    _j(payload["mentioned_actors"]),
                    payload["main_claim"],
                    _j(narratives),
                    _j(payload["target_audiences"]),
                    _j(payload["influence_methods"]),
                    _j(payload["likely_objectives"]),
                    source_role,
                    confidence,
                    payload["reason"],
                    payload["model"],
                    payload["prompt_version"],
                    run_key,
                    _j(payload),
                ),
            )

            story_eligible=bool(keep and confidence>=60 and not insufficient)
            next_status="analyzed" if story_eligible else "done"
            if keep and not story_eligible:
                review_reason=(
                    "insufficient_text" if insufficient
                    else f"confidence_below_story_threshold:{confidence}"
                )
                cur.execute(
                    """
                    INSERT INTO grok_story_review_v1(
                      source_document_id,status,reason,confidence,updated_at
                    )
                    VALUES(%s,'pending',%s,%s,now())
                    ON CONFLICT(source_document_id) DO UPDATE SET
                      status='pending',reason=excluded.reason,
                      confidence=excluded.confidence,updated_at=now()
                    """,
                    (int(document_id),review_reason,confidence),
                )
            semantic_status="pending" if story_eligible else "skipped"
            cur.execute(
                """
                UPDATE grok_analysis_run_items_v1
                   SET status=%s,semantic_status=%s,analyzed_at=now(),
                       completed_at=CASE WHEN %s='done' THEN now() ELSE NULL END
                 WHERE run_key=%s AND source_document_id=%s
                """,
                (next_status,semantic_status,next_status,run_key,int(document_id)),
            )
            cur.execute(
                """
                INSERT INTO grok_analysis_audit_v1(source_document_id,action,payload)
                VALUES(%s,'save_analysis_v2',%s::jsonb)
                """,
                (int(document_id),_j(payload)),
            )
            cur.execute(
                """
                UPDATE grok_analysis_runs_v1 r
                   SET processed_count=x.n,updated_at=now()
                  FROM (
                    SELECT count(*) FILTER(WHERE status<>'claimed')::int AS n
                      FROM grok_analysis_run_items_v1
                     WHERE run_key=%s
                  ) x
                 WHERE r.run_key=%s
                """,
                (run_key,run_key),
            )
            cur.execute("COMMIT")
            return {
                "ok":True,
                "document_id":int(document_id),
                "story_eligible":story_eligible,
                "needs_story_review":bool(keep and not story_eligible),
                "run_item_status":next_status,
                "semantic_status":semantic_status,
                "stored":payload,
            }
        except Exception:
            c.raw.rollback()
            raise
        finally:
            c.close()


    @mcp.tool()
    def apply_story_decision(run_key: str, document_id: int, decision: dict[str,Any]) -> dict:
        """Create/attach a story using one validated decision object."""
        run_key=_s(run_key,160)
        if not isinstance(decision,dict):
            raise ValueError("decision must be an object")
        action=_s(decision.get("action"),20).lower()
        if action not in {"create","attach","none"}:
            raise ValueError("decision.action must be create, attach, or none")

        c=db.connect(readonly=False)
        try:
            cur=c.raw.cursor()
            cur.execute("BEGIN")
            run=cur.execute(
                "SELECT * FROM grok_analysis_runs_v1 WHERE run_key=%s FOR UPDATE",
                (run_key,),
            ).fetchone()
            if not run or run["status"]!="open":
                raise ValueError("run_key is not an open run")
            item=cur.execute(
                """
                SELECT * FROM grok_analysis_run_items_v1
                 WHERE run_key=%s AND source_document_id=%s
                 FOR UPDATE
                """,
                (run_key,int(document_id)),
            ).fetchone()
            if not item:
                raise ValueError("document is not claimed by this run")
            analysis=cur.execute(
                """
                SELECT * FROM grok_document_analysis_v1
                 WHERE source_document_id=%s
                 FOR UPDATE
                """,
                (int(document_id),),
            ).fetchone()
            if not analysis:
                raise ValueError("save_analysis must be called first")
            if item["status"]=="done":
                cur.execute("COMMIT")
                return {
                    "ok":True,"idempotent":True,
                    "document_id":int(document_id),
                    "story_key":analysis["story_key"],
                    "novelty":analysis["novelty"],
                }
            raw=analysis["raw_result"] or {}
            insufficient=bool(raw.get("insufficient_text",False)) if isinstance(raw,dict) else False
            resolved_story_id=None
            resolved_story_key=""
            resolved_first_seen=None
            resolved_last_seen=None
            semantic_required=False

            if action=="none":
                if analysis["keep_for_analysis"] and int(analysis["confidence"] or 0)>=60 and not insufficient:
                    raise ValueError("kept high-confidence item requires create or attach")
                cur.execute(
                    """
                    UPDATE grok_analysis_run_items_v1
                       SET status='done',semantic_status='skipped',completed_at=now()
                     WHERE run_key=%s AND source_document_id=%s
                    """,
                    (run_key,int(document_id)),
                )

            elif action=="create":
                if not analysis["keep_for_analysis"]:
                    raise ValueError("noise cannot create a story")
                if int(analysis["confidence"] or 0)<60 or insufficient:
                    raise ValueError("low-confidence or insufficient-text item cannot create a new story")
                story_key=_s(decision.get("story_key"),180)
                title=_s(decision.get("title"),500)
                if not story_key or not title:
                    raise ValueError("create requires story_key and title")
                existing=cur.execute(
                    "SELECT id,canonical_document_id FROM grok_stories_v1 WHERE story_key=%s",
                    (story_key,),
                ).fetchone()
                if existing:
                    raise ValueError("story_key already exists; use action=attach")
                doc=cur.execute(
                    """
                    SELECT COALESCE(NULLIF(published_at,'')::timestamptz,
                                    NULLIF(created_at,'')::timestamptz) AS ts
                      FROM source_documents WHERE id=%s
                    """,
                    (int(document_id),),
                ).fetchone()
                narr_keys=[
                    _s(x.get("key"),180) for x in (analysis["narratives"] or [])
                    if isinstance(x,dict) and _s(x.get("key"),180)
                ]
                row=cur.execute(
                    """
                    INSERT INTO grok_stories_v1(
                      story_key,canonical_document_id,title,summary,
                      narrative_keys,target_audiences,likely_objectives,
                      first_seen_at,last_seen_at
                    )
                    VALUES(%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s)
                    RETURNING *
                    """,
                    (
                        story_key,
                        int(document_id),
                        title,
                        _s(decision.get("summary"),5000) or _s(analysis["main_claim"],5000),
                        _j(narr_keys),
                        _j(analysis["target_audiences"] or []),
                        _j(analysis["likely_objectives"] or []),
                        doc["ts"] if doc else None,
                        doc["ts"] if doc else None,
                    ),
                ).fetchone()
                resolved_story_id=int(row["id"])
                resolved_story_key=story_key
                resolved_first_seen=row["first_seen_at"]
                resolved_last_seen=row["last_seen_at"]
                semantic_required=True
                cur.execute(
                    """
                    INSERT INTO grok_story_members_v1(
                      story_id,source_document_id,relation,added_value
                    )
                    VALUES(%s,%s,'canonical','')
                    """,
                    (int(row["id"]),int(document_id)),
                )
                cur.execute(
                    """
                    UPDATE grok_document_analysis_v1
                       SET story_key=%s,novelty='new',
                           duplicate_of_document_id=NULL,added_value='',
                           updated_at=now()
                     WHERE source_document_id=%s
                    """,
                    (story_key,int(document_id)),
                )
                cur.execute(
                    "UPDATE grok_story_review_v1 SET status='resolved',updated_at=now() WHERE source_document_id=%s",
                    (int(document_id),),
                )
                cur.execute(
                    """
                    UPDATE grok_analysis_run_items_v1
                       SET status='done',completed_at=now()
                     WHERE run_key=%s AND source_document_id=%s
                    """,
                    (run_key,int(document_id)),
                )
            elif action=="attach":
                if not analysis["keep_for_analysis"]:
                    raise ValueError("noise cannot attach to a story")
                story_key=_s(decision.get("story_key"),180)
                relation=_s(decision.get("relation"),30).lower()
                added_value=_s(decision.get("added_value"),4000)
                if relation not in {"duplicate","development"}:
                    raise ValueError("attach relation must be duplicate or development")
                if relation=="development" and not added_value:
                    raise ValueError("development requires non-empty added_value")
                story=cur.execute(
                    "SELECT * FROM grok_stories_v1 WHERE story_key=%s FOR UPDATE",
                    (story_key,),
                ).fetchone()
                if not story:
                    raise ValueError("unknown story_key")
                story_has_semantic=bool(cur.execute(
                    "SELECT 1 FROM grok_story_theses_v1 WHERE story_id=%s LIMIT 1",
                    (int(story["id"]),),
                ).fetchone())
                resolved_story_id=int(story["id"])
                resolved_story_key=story_key
                resolved_first_seen=story["first_seen_at"]
                resolved_last_seen=story["last_seen_at"]
                semantic_required=not story_has_semantic
                other=cur.execute(
                    """
                    SELECT st.story_key,sm.story_id
                      FROM grok_story_members_v1 sm
                      JOIN grok_stories_v1 st ON st.id=sm.story_id
                     WHERE sm.source_document_id=%s
                    """,
                    (int(document_id),),
                ).fetchone()
                if other and int(other["story_id"])!=int(story["id"]):
                    raise ValueError(f"document already belongs to story {other['story_key']}")
                cur.execute(
                    """
                    INSERT INTO grok_story_members_v1(
                      story_id,source_document_id,relation,added_value
                    )
                    VALUES(%s,%s,%s,%s)
                    ON CONFLICT(source_document_id) DO UPDATE SET
                      relation=excluded.relation,added_value=excluded.added_value
                    """,
                    (int(story["id"]),int(document_id),relation,added_value),
                )
                cur.execute(
                    """
                    UPDATE grok_document_analysis_v1
                       SET story_key=%s,novelty=%s,duplicate_of_document_id=%s,
                           added_value=%s,updated_at=now()
                     WHERE source_document_id=%s
                    """,
                    (
                        story_key,relation,story["canonical_document_id"],
                        added_value,int(document_id),
                    ),
                )
                cur.execute(
                    """
                    UPDATE grok_stories_v1 st
                       SET first_seen_at=x.first_seen,last_seen_at=x.last_seen,updated_at=now()
                      FROM (
                        SELECT min(COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                            NULLIF(d.created_at,'')::timestamptz)) AS first_seen,
                               max(COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                            NULLIF(d.created_at,'')::timestamptz)) AS last_seen
                          FROM grok_story_members_v1 sm
                          JOIN source_documents d ON d.id=sm.source_document_id
                         WHERE sm.story_id=%s
                      ) x
                     WHERE st.id=%s
                    """,
                    (int(story["id"]),int(story["id"])),
                )
                cur.execute(
                    "UPDATE grok_story_review_v1 SET status='resolved',updated_at=now() WHERE source_document_id=%s",
                    (int(document_id),),
                )
                cur.execute(
                    """
                    UPDATE grok_analysis_run_items_v1
                       SET status='done',
                           semantic_status=%s,
                           completed_at=now()
                     WHERE run_key=%s AND source_document_id=%s
                    """,
                    ("done" if story_has_semantic else "pending",run_key,int(document_id)),
                )

            semantic_result=None
            semantic=decision.get("semantic")
            if action in {"create","attach"} and semantic_required and not isinstance(semantic,dict):
                raise ValueError(
                    "decision.semantic is required for a new story or a story without semantic memory"
                )
            if isinstance(semantic,dict):
                thesis_key=_s(semantic.get("thesis_key"),180)
                thesis_label=_s(semantic.get("thesis_label"),500)
                thesis_text=_s(semantic.get("thesis"),4000)
                thesis_description=_s(semantic.get("thesis_description"),4000)
                thesis_confidence=_i(
                    semantic.get("confidence",analysis["confidence"]),0,100
                )
                narrative_items=semantic.get("narratives")
                if not thesis_key or not thesis_label or not thesis_text:
                    raise ValueError(
                        "decision.semantic requires thesis_key, thesis_label and thesis"
                    )
                if not isinstance(narrative_items,list) or not narrative_items:
                    raise ValueError(
                        "decision.semantic.narratives must contain at least one narrative"
                    )
                first_seen=resolved_first_seen or datetime.now(timezone.utc)
                last_seen=resolved_last_seen or first_seen
                thesis=cur.execute(
                    """
                    INSERT INTO grok_theses_v1(
                      thesis_key,label,thesis,description,status,confidence,
                      first_seen_at,last_seen_at
                    )
                    VALUES(%s,%s,%s,%s,'active',%s,%s,%s)
                    ON CONFLICT(thesis_key) DO UPDATE SET
                      last_seen_at=GREATEST(
                        COALESCE(grok_theses_v1.last_seen_at,excluded.last_seen_at),
                        excluded.last_seen_at
                      ),
                      confidence=GREATEST(
                        grok_theses_v1.confidence,excluded.confidence
                      ),
                      updated_at=now()
                    RETURNING *
                    """,
                    (
                        thesis_key,thesis_label,thesis_text,thesis_description,
                        thesis_confidence,first_seen,last_seen,
                    ),
                ).fetchone()
                narrative_keys=[]
                for nd in narrative_items[:8]:
                    if not isinstance(nd,dict):
                        raise ValueError("each semantic narrative must be an object")
                    nkey=_s(nd.get("narrative_key"),180)
                    nlabel=_s(nd.get("label"),500)
                    ndesc=_s(nd.get("description"),4000)
                    nconf=_i(nd.get("confidence",thesis_confidence),0,100)
                    if not nkey or not nlabel:
                        raise ValueError(
                            "each semantic narrative requires narrative_key and label"
                        )
                    narr=cur.execute(
                        """
                        INSERT INTO grok_narratives_v1(
                          narrative_key,label,description,status,confidence,
                          first_seen_at,last_seen_at
                        )
                        VALUES(%s,%s,%s,'active',%s,%s,%s)
                        ON CONFLICT(narrative_key) DO UPDATE SET
                          last_seen_at=GREATEST(
                            COALESCE(grok_narratives_v1.last_seen_at,excluded.last_seen_at),
                            excluded.last_seen_at
                          ),
                          confidence=GREATEST(
                            grok_narratives_v1.confidence,excluded.confidence
                          ),
                          updated_at=now()
                        RETURNING *
                        """,
                        (nkey,nlabel,ndesc,nconf,first_seen,last_seen),
                    ).fetchone()
                    narrative_keys.append(nkey)
                    cur.execute(
                        """
                        INSERT INTO grok_narrative_theses_v1(
                          narrative_id,thesis_id,confidence
                        )
                        VALUES(%s,%s,%s)
                        ON CONFLICT(narrative_id,thesis_id) DO UPDATE SET
                          confidence=GREATEST(
                            grok_narrative_theses_v1.confidence,
                            excluded.confidence
                          )
                        """,
                        (int(narr["id"]),int(thesis["id"]),nconf),
                    )

                existing_primary=cur.execute(
                    """
                    SELECT t.thesis_key
                      FROM grok_story_theses_v1 st
                      JOIN grok_theses_v1 t ON t.id=st.thesis_id
                     WHERE st.story_id=%s AND st.relation='primary'
                       AND st.thesis_id<>%s
                     LIMIT 1
                    """,
                    (int(resolved_story_id),int(thesis["id"])),
                ).fetchone()
                if existing_primary:
                    raise ValueError(
                        f"story already has primary thesis {existing_primary['thesis_key']}"
                    )
                cur.execute(
                    """
                    INSERT INTO grok_story_theses_v1(
                      story_id,thesis_id,relation,confidence
                    )
                    VALUES(%s,%s,'primary',%s)
                    ON CONFLICT(story_id,thesis_id) DO UPDATE SET
                      relation='primary',
                      confidence=GREATEST(
                        grok_story_theses_v1.confidence,excluded.confidence
                      )
                    """,
                    (int(resolved_story_id),int(thesis["id"]),thesis_confidence),
                )
                cur.execute(
                    """
                    UPDATE grok_stories_v1
                       SET narrative_keys=%s::jsonb,updated_at=now()
                     WHERE id=%s
                    """,
                    (_j(narrative_keys),int(resolved_story_id)),
                )
                cur.execute(
                    """
                    UPDATE grok_analysis_run_items_v1 ri
                       SET semantic_status='done'
                      FROM grok_analysis_runs_v1 r,
                           grok_document_analysis_v1 ga
                     WHERE r.run_key=ri.run_key
                       AND r.status='open'
                       AND ga.source_document_id=ri.source_document_id
                       AND ga.story_key=%s
                       AND ri.status='done'
                    """,
                    (resolved_story_key,),
                )
                semantic_result={
                    "thesis_key":thesis_key,
                    "narrative_keys":narrative_keys,
                }

            audit={
                "run_key":run_key,
                "document_id":int(document_id),
                "action":action,
                "decision":decision,
            }
            cur.execute(
                """
                INSERT INTO grok_analysis_audit_v1(source_document_id,action,payload)
                VALUES(%s,'story_decision_v2',%s::jsonb)
                """,
                (int(document_id),_j(audit)),
            )
            cur.execute("COMMIT")
            saved=cur.execute(
                """
                SELECT source_document_id,story_key,novelty,
                       duplicate_of_document_id,added_value,confidence
                  FROM grok_document_analysis_v1
                 WHERE source_document_id=%s
                """,
                (int(document_id),),
            ).fetchone()
            return {
                "ok":True,
                "document":dict(saved) if saved else None,
                "semantic":semantic_result,
            }
        except Exception:
            c.raw.rollback()
            raise
        finally:
            c.close()


    @mcp.tool()
    def get_saved_analysis(document_id: int) -> dict:
        """Read one saved analysis and current story-review state."""
        c=db.connect(readonly=True)
        try:
            row=c.execute(
                "SELECT * FROM grok_document_analysis_v1 WHERE source_document_id=?",
                (int(document_id),),
            ).fetchone()
            review=c.execute(
                "SELECT * FROM grok_story_review_v1 WHERE source_document_id=?",
                (int(document_id),),
            ).fetchone()
            return {
                "found":bool(row),
                "analysis":dict(row) if row else None,
                "story_review":dict(review) if review else None,
            }
        finally:
            c.close()


    @mcp.tool()
    def finish_run(run_key: str) -> dict:
        """Complete a run only when every claimed item reached status=done."""
        run_key=_s(run_key,160)
        c=db.connect(readonly=False)
        try:
            cur=c.raw.cursor()
            cur.execute("BEGIN")
            run=cur.execute(
                "SELECT * FROM grok_analysis_runs_v1 WHERE run_key=%s FOR UPDATE",
                (run_key,),
            ).fetchone()
            if not run:
                raise ValueError("unknown run_key")
            rows=cur.execute(
                """
                SELECT source_document_id,ordinal,status,semantic_status
                  FROM grok_analysis_run_items_v1
                 WHERE run_key=%s
                 ORDER BY ordinal
                """,
                (run_key,),
            ).fetchall()
            pending=[
                dict(r) for r in rows
                if r["status"]!="done" or r["semantic_status"]=="pending"
            ]
            if pending:
                cur.execute("ROLLBACK")
                return {
                    "completed":False,
                    "run_key":run_key,
                    "remaining":len(pending),
                    "items":pending,
                }
            cur.execute(
                """
                UPDATE grok_analysis_runs_v1
                   SET status='completed',processed_count=claimed_count,
                       updated_at=now(),completed_at=now()
                 WHERE run_key=%s
                """,
                (run_key,),
            )
            cur.execute("COMMIT")
            return {
                "completed":True,
                "run_key":run_key,
                "claimed_count":int(run["claimed_count"]),
            }
        except Exception:
            c.raw.rollback()
            raise
        finally:
            c.close()


    def _get_semantic_matches_internal(document_id: int, limit: int = 8) -> dict:
        """Rank existing theses and narratives for a document's assigned story."""
        limit=_i(limit,1,15)
        c=db.connect(readonly=True)
        try:
            doc=c.execute(
                """
                SELECT ga.source_document_id,ga.main_claim,ga.narratives,ga.story_key,
                       st.id AS story_id,st.title AS story_title,st.summary AS story_summary
                  FROM grok_document_analysis_v1 ga
             LEFT JOIN grok_stories_v1 st ON st.story_key=ga.story_key
                 WHERE ga.source_document_id=?
                """,
                (int(document_id),),
            ).fetchone()
            if not doc or not doc["story_id"]:
                raise ValueError("document must have an assigned story first")

            existing=c.execute(
                """
                SELECT t.thesis_key,t.label AS thesis_label,t.thesis,
                       s.relation,s.confidence,
                       COALESCE(jsonb_agg(DISTINCT jsonb_build_object(
                         'key',n.narrative_key,'label',n.label
                       )) FILTER(WHERE n.id IS NOT NULL),'[]'::jsonb) AS narratives
                  FROM grok_story_theses_v1 s
                  JOIN grok_theses_v1 t ON t.id=s.thesis_id
             LEFT JOIN grok_narrative_theses_v1 nt ON nt.thesis_id=t.id
             LEFT JOIN grok_narratives_v1 n ON n.id=nt.narrative_id
                 WHERE s.story_id=?
                 GROUP BY t.id,s.relation,s.confidence
                 ORDER BY (s.relation='primary') DESC,t.id
                """,
                (int(doc["story_id"]),),
            ).fetchall()

            text=" ".join([
                _s(doc["main_claim"],4000),
                _s(doc["story_title"],500),
                _s(doc["story_summary"],5000),
            ])
            tokens=_tokens(text)
            doc_narr={
                _s(x.get("key"),180)
                for x in (doc["narratives"] or [])
                if isinstance(x,dict) and _s(x.get("key"),180)
            }
            thesis_rows=c.execute(
                """
                SELECT t.id,t.thesis_key,t.label,t.thesis,t.description,t.confidence,
                       t.last_seen_at,
                       COALESCE(jsonb_agg(DISTINCT n.narrative_key)
                         FILTER(WHERE n.id IS NOT NULL),'[]'::jsonb) AS narrative_keys,
                       count(DISTINCT st.story_id)::int AS story_count
                  FROM grok_theses_v1 t
             LEFT JOIN grok_narrative_theses_v1 nt ON nt.thesis_id=t.id
             LEFT JOIN grok_narratives_v1 n ON n.id=nt.narrative_id
             LEFT JOIN grok_story_theses_v1 st ON st.thesis_id=t.id
                 WHERE t.status='active'
                 GROUP BY t.id
                 ORDER BY t.last_seen_at DESC NULLS LAST,t.id DESC
                 LIMIT 500
                """
            ).fetchall()
            thesis_ranked=[]
            for row in thesis_rows:
                score,common=_dice(tokens,_tokens(" ".join([
                    _s(row["thesis_key"],180),
                    _s(row["label"],500),
                    _s(row["thesis"],4000),
                    _s(row["description"],3000),
                ])))
                narr_overlap=sorted(doc_narr & set(_lst(row["narrative_keys"])))
                total=min(1.0,score*0.75+(0.30 if narr_overlap else 0.0))
                thesis_ranked.append({
                    "thesis_key":row["thesis_key"],
                    "label":row["label"],
                    "thesis":row["thesis"],
                    "description":row["description"],
                    "confidence":row["confidence"],
                    "story_count":row["story_count"],
                    "narrative_keys":row["narrative_keys"],
                    "heuristic_score":round(total,4),
                    "common_terms":common,
                    "narrative_overlap":narr_overlap,
                    "last_seen_at":row["last_seen_at"],
                })
            thesis_ranked.sort(
                key=lambda x:(x["heuristic_score"],x["last_seen_at"] or datetime.min.replace(tzinfo=timezone.utc)),
                reverse=True,
            )
            narrative_rows=c.execute(
                """
                SELECT n.id,n.narrative_key,n.label,n.description,n.confidence,
                       n.last_seen_at,count(DISTINCT nt.thesis_id)::int AS thesis_count
                  FROM grok_narratives_v1 n
             LEFT JOIN grok_narrative_theses_v1 nt ON nt.narrative_id=n.id
                 WHERE n.status='active'
                 GROUP BY n.id
                 ORDER BY n.last_seen_at DESC NULLS LAST,n.id DESC
                 LIMIT 300
                """
            ).fetchall()
            narrative_ranked=[]
            for row in narrative_rows:
                score,common=_dice(tokens,_tokens(" ".join([
                    _s(row["narrative_key"],180),
                    _s(row["label"],500),
                    _s(row["description"],3000),
                ])))
                if row["narrative_key"] in doc_narr:
                    score=min(1.0,score+0.40)
                narrative_ranked.append({
                    "narrative_key":row["narrative_key"],
                    "label":row["label"],
                    "description":row["description"],
                    "confidence":row["confidence"],
                    "thesis_count":row["thesis_count"],
                    "heuristic_score":round(score,4),
                    "common_terms":common,
                    "last_seen_at":row["last_seen_at"],
                })
            narrative_ranked.sort(
                key=lambda x:(x["heuristic_score"],x["last_seen_at"] or datetime.min.replace(tzinfo=timezone.utc)),
                reverse=True,
            )
            return {
                "document_id":int(document_id),
                "story_key":doc["story_key"],
                "existing_story_semantics":[dict(r) for r in existing],
                "thesis_candidates":thesis_ranked[:limit],
                "narrative_candidates":narrative_ranked[:limit],
                "note":"Candidates are retrieval hints only; reuse only when semantically equivalent.",
            }
        finally:
            c.close()


    def _apply_semantic_decision_internal(run_key: str, document_id: int, decision: dict[str,Any]) -> dict:
        """Create/reuse Thesis and Narrative memory for an assigned story."""
        run_key=_s(run_key,160)
        if not isinstance(decision,dict):
            raise ValueError("decision must be an object")
        thesis_decision=decision.get("thesis")
        narratives_decision=decision.get("narratives")
        if not isinstance(thesis_decision,dict):
            raise ValueError("decision.thesis is required")
        if not isinstance(narratives_decision,list) or not narratives_decision:
            raise ValueError("decision.narratives must contain at least one narrative")
        relation=_s(decision.get("relation","primary"),20).lower() or "primary"
        if relation not in {"primary","secondary"}:
            raise ValueError("relation must be primary or secondary")

        c=db.connect(readonly=False)
        try:
            cur=c.raw.cursor()
            cur.execute("BEGIN")
            run=cur.execute(
                "SELECT * FROM grok_analysis_runs_v1 WHERE run_key=%s FOR UPDATE",
                (run_key,),
            ).fetchone()
            if not run or run["status"]!="open":
                raise ValueError("run_key is not an open run")
            item=cur.execute(
                """
                SELECT * FROM grok_analysis_run_items_v1
                 WHERE run_key=%s AND source_document_id=%s
                 FOR UPDATE
                """,
                (run_key,int(document_id)),
            ).fetchone()
            if not item:
                raise ValueError("document is not claimed by this run")
            if item["status"]!="done":
                raise ValueError("story decision must be completed first")
            if item["semantic_status"]=="done":
                cur.execute("COMMIT")
                return {"ok":True,"idempotent":True,"document_id":int(document_id)}
            analysis=cur.execute(
                """
                SELECT ga.*,st.id AS story_id,st.first_seen_at,st.last_seen_at
                  FROM grok_document_analysis_v1 ga
                  JOIN grok_stories_v1 st ON st.story_key=ga.story_key
                 WHERE ga.source_document_id=%s
                 FOR UPDATE OF ga,st
                """,
                (int(document_id),),
            ).fetchone()
            if not analysis:
                raise ValueError("document must have an assigned story")

            first_seen=analysis["first_seen_at"] or datetime.now(timezone.utc)
            last_seen=analysis["last_seen_at"] or first_seen

            thesis_action=_s(thesis_decision.get("action"),20).lower()
            thesis_key=_s(thesis_decision.get("thesis_key"),180)
            thesis_label=_s(thesis_decision.get("label"),500)
            thesis_text=_s(thesis_decision.get("thesis"),4000)
            thesis_description=_s(thesis_decision.get("description"),4000)
            thesis_confidence=_i(thesis_decision.get("confidence",analysis["confidence"]),0,100)
            if thesis_action not in {"create","reuse"}:
                raise ValueError("thesis.action must be create or reuse")
            if not thesis_key:
                raise ValueError("thesis_key is required")

            if thesis_action=="reuse":
                thesis=cur.execute(
                    "SELECT * FROM grok_theses_v1 WHERE thesis_key=%s AND status='active' FOR UPDATE",
                    (thesis_key,),
                ).fetchone()
                if not thesis:
                    raise ValueError("unknown active thesis_key for reuse")
                cur.execute(
                    """
                    UPDATE grok_theses_v1
                       SET last_seen_at=GREATEST(COALESCE(last_seen_at,%s),%s),
                           confidence=GREATEST(confidence,%s),updated_at=now()
                     WHERE id=%s
                    """,
                    (last_seen,last_seen,thesis_confidence,int(thesis["id"])),
                )
            else:
                if not thesis_label or not thesis_text:
                    raise ValueError("create thesis requires label and thesis")
                thesis=cur.execute(
                    """
                    INSERT INTO grok_theses_v1(
                      thesis_key,label,thesis,description,status,confidence,
                      first_seen_at,last_seen_at
                    )
                    VALUES(%s,%s,%s,%s,'active',%s,%s,%s)
                    ON CONFLICT(thesis_key) DO UPDATE SET
                      last_seen_at=GREATEST(
                        COALESCE(grok_theses_v1.last_seen_at,excluded.last_seen_at),
                        excluded.last_seen_at
                      ),
                      confidence=GREATEST(grok_theses_v1.confidence,excluded.confidence),
                      updated_at=now()
                    RETURNING *
                    """,
                    (
                        thesis_key,thesis_label,thesis_text,thesis_description,
                        thesis_confidence,first_seen,last_seen,
                    ),
                ).fetchone()
            narrative_keys=[]
            for nd in narratives_decision[:8]:
                if not isinstance(nd,dict):
                    raise ValueError("each narrative decision must be an object")
                action=_s(nd.get("action"),20).lower()
                key=_s(nd.get("narrative_key"),180)
                label=_s(nd.get("label"),500)
                description=_s(nd.get("description"),4000)
                confidence=_i(nd.get("confidence",thesis_confidence),0,100)
                if action not in {"create","reuse"}:
                    raise ValueError("narrative.action must be create or reuse")
                if not key:
                    raise ValueError("narrative_key is required")
                if action=="reuse":
                    narr=cur.execute(
                        "SELECT * FROM grok_narratives_v1 WHERE narrative_key=%s AND status='active' FOR UPDATE",
                        (key,),
                    ).fetchone()
                    if not narr:
                        raise ValueError(f"unknown active narrative_key for reuse: {key}")
                    cur.execute(
                        """
                        UPDATE grok_narratives_v1
                           SET last_seen_at=GREATEST(COALESCE(last_seen_at,%s),%s),
                               confidence=GREATEST(confidence,%s),updated_at=now()
                         WHERE id=%s
                        """,
                        (last_seen,last_seen,confidence,int(narr["id"])),
                    )
                else:
                    if not label:
                        raise ValueError("create narrative requires label")
                    narr=cur.execute(
                        """
                        INSERT INTO grok_narratives_v1(
                          narrative_key,label,description,status,confidence,
                          first_seen_at,last_seen_at
                        )
                        VALUES(%s,%s,%s,'active',%s,%s,%s)
                        ON CONFLICT(narrative_key) DO UPDATE SET
                          last_seen_at=GREATEST(
                            COALESCE(grok_narratives_v1.last_seen_at,excluded.last_seen_at),
                            excluded.last_seen_at
                          ),
                          confidence=GREATEST(grok_narratives_v1.confidence,excluded.confidence),
                          updated_at=now()
                        RETURNING *
                        """,
                        (key,label,description,confidence,first_seen,last_seen),
                    ).fetchone()
                narrative_keys.append(key)
                cur.execute(
                    """
                    INSERT INTO grok_narrative_theses_v1(
                      narrative_id,thesis_id,confidence
                    )
                    VALUES(%s,%s,%s)
                    ON CONFLICT(narrative_id,thesis_id) DO UPDATE SET
                      confidence=GREATEST(
                        grok_narrative_theses_v1.confidence,excluded.confidence
                      )
                    """,
                    (int(narr["id"]),int(thesis["id"]),confidence),
                )
            if relation=="primary":
                existing_primary=cur.execute(
                    """
                    SELECT t.thesis_key
                      FROM grok_story_theses_v1 st
                      JOIN grok_theses_v1 t ON t.id=st.thesis_id
                     WHERE st.story_id=%s AND st.relation='primary'
                       AND st.thesis_id<>%s
                     LIMIT 1
                    """,
                    (int(analysis["story_id"]),int(thesis["id"])),
                ).fetchone()
                if existing_primary:
                    raise ValueError(
                        f"story already has primary thesis {existing_primary['thesis_key']}"
                    )

            cur.execute(
                """
                INSERT INTO grok_story_theses_v1(
                  story_id,thesis_id,relation,confidence
                )
                VALUES(%s,%s,%s,%s)
                ON CONFLICT(story_id,thesis_id) DO UPDATE SET
                  relation=excluded.relation,
                  confidence=GREATEST(grok_story_theses_v1.confidence,excluded.confidence)
                """,
                (
                    int(analysis["story_id"]),int(thesis["id"]),
                    relation,thesis_confidence,
                ),
            )
            cur.execute(
                """
                UPDATE grok_stories_v1
                   SET narrative_keys=%s::jsonb,updated_at=now()
                 WHERE id=%s
                """,
                (_j(narrative_keys),int(analysis["story_id"])),
            )
            cur.execute(
                """
                UPDATE grok_analysis_run_items_v1 ri
                   SET semantic_status='done'
                  FROM grok_analysis_runs_v1 r,
                       grok_document_analysis_v1 ga
                 WHERE r.run_key=ri.run_key
                   AND r.status='open'
                   AND ga.source_document_id=ri.source_document_id
                   AND ga.story_key=%s
                   AND ri.status='done'
                """,
                (analysis["story_key"],),
            )

            payload={
                "run_key":run_key,
                "document_id":int(document_id),
                "story_key":analysis["story_key"],
                "thesis_key":thesis_key,
                "narrative_keys":narrative_keys,
                "relation":relation,
                "decision":decision,
            }
            cur.execute(
                """
                INSERT INTO grok_analysis_audit_v1(source_document_id,action,payload)
                VALUES(%s,'semantic_decision_v1',%s::jsonb)
                """,
                (int(document_id),_j(payload)),
            )
            cur.execute("COMMIT")
            return {
                "ok":True,
                "document_id":int(document_id),
                "story_key":analysis["story_key"],
                "thesis_key":thesis_key,
                "narrative_keys":narrative_keys,
                "semantic_status":"done",
            }
        except Exception:
            c.raw.rollback()
            raise
        finally:
            c.close()
