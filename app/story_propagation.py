from typing import Any

from app import db

VERSION = "primary-exact-propagation-v1-20260930"


def sync_exact_primary(connection: db.Connection) -> dict[str, int]:
    rows = connection.execute("""
        WITH cluster_story AS (
            SELECT pc.cluster_id,min(sm.story_id)::bigint AS story_id,
                   count(DISTINCT sm.story_id)::int AS stories
              FROM primary_info_cluster_members_v1 pc
              JOIN grok_story_members_v1 sm ON sm.source_document_id=pc.source_document_id
             GROUP BY pc.cluster_id
        ), candidates AS (
            SELECT DISTINCT cs.story_id,pm.source_document_id
              FROM cluster_story cs
              JOIN primary_info_cluster_members_v1 pm ON pm.cluster_id=cs.cluster_id
         LEFT JOIN grok_story_members_v1 existing ON existing.source_document_id=pm.source_document_id
             WHERE cs.stories=1 AND existing.source_document_id IS NULL
               AND pm.match_type='exact' AND pm.similarity>=0.999999
        )
        INSERT INTO grok_story_members_v1(story_id,source_document_id,relation,added_value)
        SELECT story_id,source_document_id,'duplicate','primary_exact_propagation'
          FROM candidates
        ON CONFLICT(source_document_id) DO NOTHING
        RETURNING story_id,source_document_id
    """).fetchall()
    touched = sorted({int(r["story_id"]) for r in rows})
    for story_id in touched:
        bounds = connection.execute("""
            SELECT min(COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                NULLIF(d.created_at,'')::timestamptz)) AS first_seen,
                   max(COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                NULLIF(d.created_at,'')::timestamptz)) AS last_seen
              FROM grok_story_members_v1 sm
              JOIN source_documents d ON d.id=sm.source_document_id
             WHERE sm.story_id=?
        """, (story_id,)).fetchone()
        connection.execute("""
            UPDATE grok_stories_v1
               SET first_seen_at=?,last_seen_at=?,updated_at=now()
             WHERE id=?
        """, (bounds.get("first_seen"), bounds.get("last_seen"), story_id))
    return {"inserted": len(rows), "stories_touched": len(touched)}


def sync_now() -> dict[str, Any]:
    connection = db.connect()
    try:
        result = sync_exact_primary(connection)
        connection.commit()
        result["version"] = VERSION
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
