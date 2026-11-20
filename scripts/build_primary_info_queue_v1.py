import json
from collections import defaultdict
from datetime import datetime, timezone
from app import db
from app import primary_dedup

CUTOFF="2026-09-13 21:01:00+00"
VERSION="info-primary-v1-gold-20260915"

c=db.connect(readonly=False)
try:
    c.raw.execute("""
    CREATE TABLE IF NOT EXISTS primary_info_clusters_v1 (
      id bigserial PRIMARY KEY,
      canonical_document_id bigint NOT NULL UNIQUE REFERENCES source_documents(id) ON DELETE CASCADE,
      classifier_version text NOT NULL DEFAULT '',
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      spread_count integer NOT NULL DEFAULT 1,
      unique_sources integer NOT NULL DEFAULT 1,
      first_seen_at timestamptz,
      last_seen_at timestamptz,
      source_groups jsonb NOT NULL DEFAULT '[]'::jsonb,
      audiences jsonb NOT NULL DEFAULT '[]'::jsonb,
      country_spaces jsonb NOT NULL DEFAULT '[]'::jsonb,
      representative_sources jsonb NOT NULL DEFAULT '[]'::jsonb
    )""")
    c.raw.execute("""
    CREATE TABLE IF NOT EXISTS primary_info_cluster_members_v1 (
      cluster_id bigint NOT NULL REFERENCES primary_info_clusters_v1(id) ON DELETE CASCADE,
      source_document_id bigint NOT NULL UNIQUE REFERENCES source_documents(id) ON DELETE CASCADE,
      match_type text NOT NULL,
      similarity double precision NOT NULL DEFAULT 1.0,
      propagation_role text NOT NULL DEFAULT 'repeater',
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(cluster_id, source_document_id)
    )""")
    c.raw.execute("""
    CREATE TABLE IF NOT EXISTS primary_info_queue_v1 (
      source_document_id bigint PRIMARY KEY REFERENCES source_documents(id) ON DELETE CASCADE,
      cluster_id bigint REFERENCES primary_info_clusters_v1(id) ON DELETE CASCADE,
      canonical_document_id bigint NOT NULL REFERENCES source_documents(id) ON DELETE CASCADE,
      status text NOT NULL DEFAULT 'pending',
      decision text NOT NULL DEFAULT '',
      batch_id text NOT NULL DEFAULT '',
      attempts integer NOT NULL DEFAULT 0,
      locked_at timestamptz,
      retry_at timestamptz,
      classifier_version text NOT NULL DEFAULT '',
      reason text NOT NULL DEFAULT '',
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      completed_at timestamptz
    )""")
    c.raw.execute("CREATE INDEX IF NOT EXISTS idx_primary_info_queue_v1_status ON primary_info_queue_v1(status,created_at)")
    c.raw.execute("CREATE INDEX IF NOT EXISTS idx_primary_info_members_v1_cluster ON primary_info_cluster_members_v1(cluster_id)")
    c.commit()

    rows=c.execute("""
    SELECT d.id,d.source_id,d.source_kind,d.source_title,d.source_url,d.source_text,d.full_text,
           d.published_at,d.created_at,d.content_hash,
           COALESCE(s.groups,'[]'::jsonb) AS groups,
           COALESCE(s.audiences,'[]'::jsonb) AS audiences,
           COALESCE(s.country_spaces,'[]'::jsonb) AS country_spaces
      FROM source_documents d
 LEFT JOIN sources s ON s.id=d.source_id
     WHERE NULLIF(d.created_at,'')::timestamptz >= ?::timestamptz
     ORDER BY NULLIF(d.created_at,'')::timestamptz, d.id
    """,(CUTOFF,)).fetchall()

    existing={int(r["source_document_id"]):int(r["cluster_id"]) for r in c.execute(
      "SELECT source_document_id,cluster_id FROM primary_info_cluster_members_v1"
    ).fetchall()}

    exact_map={}
    near_buckets=defaultdict(list)
    stats=defaultdict(int)

    def tstamp(r):
        return r["published_at"] or r["created_at"]

    def ensure_cluster(r):
        row=c.execute("SELECT id FROM primary_info_clusters_v1 WHERE canonical_document_id=?",(int(r["id"]),)).fetchone()
        if row: return int(row["id"])
        c.execute("""INSERT INTO primary_info_clusters_v1
          (canonical_document_id,classifier_version,first_seen_at,last_seen_at,source_groups,audiences,country_spaces,representative_sources)
          VALUES(?,?,NULLIF(?,'')::timestamptz,NULLIF(?,'')::timestamptz,?::jsonb,?::jsonb,?::jsonb,?::jsonb)""",
          (int(r["id"]),VERSION,tstamp(r),tstamp(r),
           json.dumps(r["groups"] or [],ensure_ascii=False),
           json.dumps(r["audiences"] or [],ensure_ascii=False),
           json.dumps(r["country_spaces"] or [],ensure_ascii=False),
           json.dumps([{"source_id":r["source_id"],"title":r["source_title"],"url":r["source_url"]}],ensure_ascii=False)))
        return int(c.execute("SELECT id FROM primary_info_clusters_v1 WHERE canonical_document_id=?",(int(r["id"]),)).fetchone()["id"])

    def add_member(cid,r,match_type,sim,role):
        c.execute("""INSERT INTO primary_info_cluster_members_v1(cluster_id,source_document_id,match_type,similarity,propagation_role)
                     VALUES(?,?,?,?,?) ON CONFLICT(source_document_id) DO NOTHING""",
                  (cid,int(r["id"]),match_type,float(sim),role))

    for idx,r in enumerate(rows):
        did=int(r["id"])
        if did in existing:
            stats["existing"]+=1
            continue
        text=(r["full_text"] or r["source_text"] or "").strip()
        junk=primary_dedup.quick_junk_reason(text)
        if junk:
            cid=ensure_cluster(r)
            add_member(cid,r,"local_junk",1.0,"origin")
            c.execute("""INSERT INTO primary_info_queue_v1(source_document_id,cluster_id,canonical_document_id,status,decision,classifier_version,reason,completed_at)
                         VALUES(?,?,?,'completed','NOT_RELEVANT',?,?,now())
                         ON CONFLICT(source_document_id) DO NOTHING""",
                      (did,cid,did,VERSION,"Швидкий фільтр: "+junk))
            stats["junk"]+=1
            continue

        norm_hash=primary_dedup.normalized_hash(text)
        if norm_hash and norm_hash in exact_map:
            cid,canon=exact_map[norm_hash]
            add_member(cid,r,"exact",1.0,"repeater")
            c.execute("""INSERT INTO primary_info_queue_v1(source_document_id,cluster_id,canonical_document_id,status,decision,classifier_version,reason,completed_at)
                         VALUES(?,?,?,'clustered','INHERIT',?,?,now())
                         ON CONFLICT(source_document_id) DO NOTHING""",
                      (did,cid,canon,VERSION,"exact duplicate; inherit canonical analysis"))
            stats["exact"]+=1
            continue

        tokens=primary_dedup.meaningful_tokens(text)
        bucket=None
        if len(tokens)>=30 and len(primary_dedup.normalize_text(text))>=220:
            simhash=primary_dedup._simhash64(tokens)
            bucket=(simhash>>48)&0xFFFF
        best=None
        if bucket is not None:
            for prev in reversed(near_buckets[bucket][-80:]):
                try:
                    dt1=datetime.fromisoformat(str(tstamp(r)).replace("Z","+00:00"))
                    dt0=datetime.fromisoformat(str(prev["ts"]).replace("Z","+00:00"))
                    if dt1.tzinfo is None: dt1=dt1.replace(tzinfo=timezone.utc)
                    if dt0.tzinfo is None: dt0=dt0.replace(tzinfo=timezone.utc)
                    if abs((dt1-dt0).total_seconds())>36*3600: continue
                except Exception:
                    pass
                sim=primary_dedup.near_similarity(text,prev["text"])
                if sim>=0.92 and (best is None or sim>best[0]):
                    best=(sim,prev)
        if best:
            sim,prev=best
            cid=prev["cluster_id"]; canon=prev["canonical_id"]
            add_member(cid,r,"near",sim,"repeater")
            c.execute("""INSERT INTO primary_info_queue_v1(source_document_id,cluster_id,canonical_document_id,status,decision,classifier_version,reason,completed_at)
                         VALUES(?,?,?,'clustered','INHERIT',?,?,now())
                         ON CONFLICT(source_document_id) DO NOTHING""",
                      (did,cid,canon,VERSION,f"near duplicate {sim:.3f}; inherit canonical analysis"))
            stats["near"]+=1
            continue

        cid=ensure_cluster(r)
        add_member(cid,r,"canonical",1.0,"origin")
        c.execute("""INSERT INTO primary_info_queue_v1(source_document_id,cluster_id,canonical_document_id,status,decision,classifier_version,reason)
                     VALUES(?,?,?,'pending','',?,'')
                     ON CONFLICT(source_document_id) DO NOTHING""",(did,cid,did,VERSION))
        exact_map[norm_hash]=(cid,did)
        if bucket is not None:
            near_buckets[bucket].append({"cluster_id":cid,"canonical_id":did,"text":text,"ts":tstamp(r)})
        stats["canonical"]+=1
        if idx and idx%1000==0:
            c.commit()

    c.commit()

    for rr in c.execute("SELECT id FROM primary_info_clusters_v1 ORDER BY id").fetchall():
        cid=int(rr["id"])
        members=c.execute("""
        SELECT d.source_id,d.source_title,d.source_url,
               COALESCE(s.groups,'[]'::jsonb) groups,COALESCE(s.audiences,'[]'::jsonb) audiences,
               COALESCE(s.country_spaces,'[]'::jsonb) country_spaces
          FROM primary_info_cluster_members_v1 m
          JOIN source_documents d ON d.id=m.source_document_id
     LEFT JOIN sources s ON s.id=d.source_id
         WHERE m.cluster_id=?
        """,(cid,)).fetchall()
        if not members: continue
        src_ids={m["source_id"] for m in members if m["source_id"] is not None}
        groups=sorted({x for m in members for x in (m["groups"] or [])})
        aud=sorted({x for m in members for x in (m["audiences"] or [])})
        spaces=sorted({x for m in members for x in (m["country_spaces"] or [])})
        reps=[]; seen=set()
        for m in members:
            key=m["source_id"] or m["source_title"]
            if key in seen: continue
            seen.add(key); reps.append({"source_id":m["source_id"],"title":m["source_title"],"url":m["source_url"]})
            if len(reps)>=8: break
        c.execute("""UPDATE primary_info_clusters_v1 SET spread_count=?,unique_sources=?,
                     first_seen_at=(SELECT min(COALESCE(NULLIF(d.published_at,'')::timestamptz,NULLIF(d.created_at,'')::timestamptz))
                                      FROM primary_info_cluster_members_v1 mm JOIN source_documents d ON d.id=mm.source_document_id WHERE mm.cluster_id=?),
                     last_seen_at=(SELECT max(COALESCE(NULLIF(d.published_at,'')::timestamptz,NULLIF(d.created_at,'')::timestamptz))
                                      FROM primary_info_cluster_members_v1 mm JOIN source_documents d ON d.id=mm.source_document_id WHERE mm.cluster_id=?),
                     source_groups=?::jsonb,audiences=?::jsonb,country_spaces=?::jsonb,representative_sources=?::jsonb,updated_at=now()
                     WHERE id=?""",
                  (len(members),len(src_ids) or 1,cid,cid,json.dumps(groups,ensure_ascii=False),json.dumps(aud,ensure_ascii=False),json.dumps(spaces,ensure_ascii=False),json.dumps(reps,ensure_ascii=False),cid))
    c.commit()

    out={
      "input_rows":len(rows),
      "stats":dict(stats),
      "queue_status":[dict(r) for r in c.execute("SELECT status,count(*) n FROM primary_info_queue_v1 GROUP BY status ORDER BY n DESC").fetchall()],
      "pending":c.execute("SELECT count(*) n FROM primary_info_queue_v1 WHERE status='pending'").fetchone()["n"],
      "clusters":c.execute("SELECT count(*) n FROM primary_info_clusters_v1").fetchone()["n"],
      "spread_gt1":c.execute("SELECT count(*) n FROM primary_info_clusters_v1 WHERE spread_count>1").fetchone()["n"],
      "top_spread":[dict(r) for r in c.execute("""
      SELECT cl.id,cl.canonical_document_id,cl.spread_count,cl.unique_sources,cl.source_groups,cl.audiences,cl.country_spaces,d.source_title,left(d.source_text,220) sample
      FROM primary_info_clusters_v1 cl JOIN source_documents d ON d.id=cl.canonical_document_id
      ORDER BY cl.spread_count DESC,cl.unique_sources DESC LIMIT 15
      """).fetchall()]
    }
    print(json.dumps(out,ensure_ascii=True,default=str))
finally:
    c.close()
