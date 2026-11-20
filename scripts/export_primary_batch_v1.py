import json, uuid, sys
from pathlib import Path
from datetime import datetime, timezone
from app import db

MAX_ITEMS=50
MAX_CHARS=60000
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/"runtime"/"primary_batches"/"inbox"
OUT.mkdir(parents=True,exist_ok=True)

c=db.connect(readonly=False)
try:
    # Atomic claim: lock only rows this worker will reserve.
    # SKIP LOCKED prevents parallel workers from ever taking the same canonical item.
    cur=c.raw.cursor()
    cur.execute("BEGIN")
    cur.execute("""
      WITH picked AS (
        SELECT q.source_document_id
          FROM primary_info_queue_v1 q
          JOIN source_documents d ON d.id=q.source_document_id
         WHERE q.status='pending'
         ORDER BY COALESCE(NULLIF(d.published_at,'')::timestamptz,NULLIF(d.created_at,'')::timestamptz),d.id
         FOR UPDATE OF q SKIP LOCKED
         LIMIT 250
      )
      SELECT q.source_document_id,q.cluster_id,d.source_kind,d.source_title,d.source_url,
             d.source_text,d.full_text,d.published_at,d.created_at,
             COALESCE(s.groups,'[]'::jsonb) groups,
             COALESCE(s.audiences,'[]'::jsonb) audiences,
             COALESCE(s.country_spaces,'[]'::jsonb) country_spaces,
             cl.spread_count,cl.unique_sources,cl.representative_sources
        FROM picked p
        JOIN primary_info_queue_v1 q ON q.source_document_id=p.source_document_id
        JOIN source_documents d ON d.id=q.source_document_id
   LEFT JOIN sources s ON s.id=d.source_id
        JOIN primary_info_clusters_v1 cl ON cl.id=q.cluster_id
       ORDER BY COALESCE(NULLIF(d.published_at,'')::timestamptz,NULLIF(d.created_at,'')::timestamptz),d.id
    """)
    rows=cur.fetchall()
    # app.db uses psycopg dict_row, so rows are already mapping-like.
    # Keep compatibility with tuple-row connections as well.
    if rows and not hasattr(rows[0], "keys"):
        cols=[d.name for d in cur.description]
        rows=[dict(zip(cols,r)) for r in rows]

    items=[]; chars=0
    for r in rows:
        text=(r["source_text"] or "").strip()
        if len(text)>7000: text=text[:7000]
        est=len(text)
        if items and (len(items)>=MAX_ITEMS or chars+est>MAX_CHARS): break
        items.append({
          "source_document_id":int(r["source_document_id"]),
          "cluster_id":int(r["cluster_id"]),
          "source_kind":r["source_kind"],
          "source":r["source_title"],
          "url":r["source_url"],
          "published_at":r["published_at"],
          "created_at":r["created_at"],
          "groups":r["groups"] or [],
          "audience_class":r["audiences"] or [],
          "country_spaces":r["country_spaces"] or [],
          "spread_count":int(r["spread_count"] or 1),
          "unique_sources":int(r["unique_sources"] or 1),
          "representative_sources":r["representative_sources"] or [],
          "text":text
        })
        chars+=est

    if not items:
        cur.execute("ROLLBACK")
        print(json.dumps({"status":"empty"})); raise SystemExit

    batch_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"_"+uuid.uuid4().hex[:8]
    ids=[x["source_document_id"] for x in items]
    cur.execute("""
      UPDATE primary_info_queue_v1
         SET status='ready', batch_id=%s, locked_at=now(), updated_at=now()
       WHERE source_document_id = ANY(%s) AND status='pending'
    """,(batch_id,ids))
    if cur.rowcount != len(ids):
        cur.execute("ROLLBACK")
        raise RuntimeError(f"atomic reservation mismatch: wanted {len(ids)}, got {cur.rowcount}")
    cur.execute("COMMIT")

    payload={
      "batch_id":batch_id,
      "classifier_version":"info-primary-v1-gold-20260915",
      "created_at":datetime.now(timezone.utc).isoformat(),
      "item_count":len(items),
      "chars":chars,
      "items":items
    }
    path=OUT/f"{batch_id}.json"
    path.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"batch_id":batch_id,"path":str(path),"items":len(items),"chars":chars},ensure_ascii=True))
finally:
    c.close()
