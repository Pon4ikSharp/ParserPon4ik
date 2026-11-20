import json, sys
from pathlib import Path
from app import db

ROOT=Path(__file__).resolve().parents[1]
path=Path(sys.argv[1]) if len(sys.argv)>1 else None
if not path or not path.exists():
    raise SystemExit("result file required")

payload=json.loads(path.read_text(encoding="utf-8"))
batch_id=payload["batch_id"]
version=payload.get("classifier_version","")
results=payload["results"]

ALLOWED={"RELEVANT","WATCH","CONTEXT_TRIGGER","NEEDS_FULL_TEXT","NOT_RELEVANT"}
REQUIRED_COMMON={"source_document_id","decision","confidence","reason"}
REQUIRED_ANALYTIC={
    "weekly_blocks","weekly_subblocks","audience_class","country_spaces",
    "information_trigger","actor","actor_level","official_statement",
    "international_platform","claim","narrative_key","narrative_title",
    "framing","intended_effect","target_entities","techniques","threat",
    "nuclear_rhetoric","scaling","official_legitimization",
    "country_adaptation","future_relevance","needs_full_text","reason"
}
TEXT_FIELDS_FOR_ENCODING_CHECK={
    "reason","framing","intended_effect","future_relevance","narrative_title"
}

def has_mojibake(value):
    if value is None:
        return False
    if isinstance(value,list):
        return any(has_mojibake(v) for v in value)
    if not isinstance(value,str):
        return False
    score=value.count("Ð")+value.count("Ñ")+value.count("Ã")+value.count("Â")
    return score >= 3

def validate_result(res):
    if not isinstance(res,dict):
        raise RuntimeError("each result must be an object")
    missing=sorted(REQUIRED_COMMON-set(res))
    if missing:
        raise RuntimeError(f"result missing common fields: {missing}")
    decision=str(res.get("decision") or "").strip()
    if decision not in ALLOWED:
        raise RuntimeError(f"invalid decision {decision!r}")
    try:
        conf=int(res.get("confidence"))
    except Exception as exc:
        raise RuntimeError("confidence must be integer") from exc
    if not 0 <= conf <= 100:
        raise RuntimeError(f"confidence out of range: {conf}")
    if decision!="NOT_RELEVANT":
        missing=sorted(REQUIRED_ANALYTIC-set(res))
        if missing:
            raise RuntimeError(
                f"incomplete analytic result for {res.get('source_document_id')}: missing {missing}. "
                "Do not use heuristic/non-loss shortcut; re-run full PRIMARY_INFO_V1."
            )
        if decision=="RELEVANT" and not str(res.get("narrative_key") or "").strip():
            raise RuntimeError(f"RELEVANT result {res.get('source_document_id')} has empty narrative_key")
        trigger=" ".join(str(res.get("information_trigger") or "").split())
        if len(trigger)>320:
            raise RuntimeError(f"information_trigger too long for {res.get('source_document_id')}: {len(trigger)} chars")
        claim=" ".join(str(res.get("claim") or "").split())
        if trigger and claim and trigger.lower()==claim.lower():
            raise RuntimeError(f"information_trigger duplicates claim for {res.get('source_document_id')}")
    for field in TEXT_FIELDS_FOR_ENCODING_CHECK:
        if field in res and has_mojibake(res.get(field)):
            raise RuntimeError(
                f"mojibake/encoding corruption in {field} for {res.get('source_document_id')}; "
                "re-run the full batch with UTF-8 output"
            )
    if has_mojibake(res.get("weekly_blocks")) or has_mojibake(res.get("target_entities")):
        raise RuntimeError(
            f"mojibake/encoding corruption in structured analysis for {res.get('source_document_id')}"
        )

if not isinstance(results,list) or not results:
    raise RuntimeError("results must be a non-empty list")
if len(results)>50:
    raise RuntimeError(f"batch too large: {len(results)} > 50")
for res in results:
    validate_result(res)
ids=[int(r["source_document_id"]) for r in results]
if len(ids)!=len(set(ids)):
    raise RuntimeError("duplicate source_document_id in result batch")

c=db.connect(readonly=False)
try:
    expected=[int(r["source_document_id"]) for r in c.execute(
        "SELECT source_document_id FROM primary_info_queue_v1 WHERE batch_id=? ORDER BY source_document_id",
        (batch_id,)
    ).fetchall()]
    if sorted(expected)!=sorted(ids):
        raise RuntimeError(
            f"batch coverage mismatch: expected {len(expected)} queue rows, received {len(ids)} results"
        )

    c.raw.execute("""
    CREATE TABLE IF NOT EXISTS primary_info_results_v1 (
      source_document_id bigint PRIMARY KEY REFERENCES source_documents(id) ON DELETE CASCADE,
      cluster_id bigint REFERENCES primary_info_clusters_v1(id) ON DELETE CASCADE,
      classifier_version text NOT NULL,
      decision text NOT NULL,
      confidence integer NOT NULL DEFAULT 0,
      result jsonb NOT NULL DEFAULT '{}'::jsonb,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    )""")
    imported=0
    for res in results:
        did=int(res["source_document_id"])
        q=c.execute("SELECT cluster_id,status,batch_id FROM primary_info_queue_v1 WHERE source_document_id=?",(did,)).fetchone()
        if not q:
            raise RuntimeError(f"queue row missing for {did}")
        if q["batch_id"] != batch_id:
            raise RuntimeError(f"batch mismatch for {did}: {q['batch_id']} != {batch_id}")
        decision=str(res["decision"]).strip()
        confidence=int(res["confidence"])
        c.execute("""INSERT INTO primary_info_results_v1
          (source_document_id,cluster_id,classifier_version,decision,confidence,result,updated_at)
          VALUES(?,?,?,?,?,?::jsonb,now())
          ON CONFLICT(source_document_id) DO UPDATE SET
            cluster_id=excluded.cluster_id,classifier_version=excluded.classifier_version,
            decision=excluded.decision,confidence=excluded.confidence,result=excluded.result,updated_at=now()
        """,(did,int(q["cluster_id"]),version,decision,confidence,json.dumps(res,ensure_ascii=False)))
        c.execute("""UPDATE primary_info_queue_v1
                     SET status='completed',decision=?,classifier_version=?,reason=?,updated_at=now(),completed_at=now()
                     WHERE source_document_id=?""",
                  (decision,version,str(res.get("reason") or ""),did))
        imported+=1
    c.commit()
    summary=[dict(r) for r in c.execute("""
      SELECT decision,count(*) n FROM primary_info_results_v1
      GROUP BY decision ORDER BY n DESC
    """).fetchall()]
    print(json.dumps({"batch_id":batch_id,"imported":imported,"decisions":summary},ensure_ascii=False))
finally:
    c.close()
