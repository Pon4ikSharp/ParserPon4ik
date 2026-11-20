from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db
from scripts.sync_primary_info_queue_v1 import (
    VERSION,
    exact_window_hours,
    update_cluster_and_prefilter,
)


def split_groups(rows):
    if not rows:
        return []
    text = (rows[0]["full_text"] or rows[0]["source_text"] or "").strip()
    hours = exact_window_hours(text)
    groups = []
    current = []
    group_start = None
    for row in rows:
        event_at = row["event_at"]
        if not current:
            current = [row]
            group_start = event_at
            continue
        age_h = (event_at - group_start).total_seconds() / 3600.0
        if age_h <= hours:
            current.append(row)
        else:
            groups.append(current)
            current = [row]
            group_start = event_at
    if current:
        groups.append(current)
    return groups


def load_candidates(connection, limit=0):
    sql = """
      SELECT m.cluster_id
        FROM primary_info_cluster_members_v1 m
        JOIN source_documents d ON d.id=m.source_document_id
       WHERE m.match_type IN ('canonical','exact')
       GROUP BY m.cluster_id
      HAVING count(*)>1
         AND max(length(trim(COALESCE(NULLIF(d.full_text,''),d.source_text,''))))<=160
         AND extract(epoch FROM (
               max(COALESCE(NULLIF(d.published_at,'')::timestamptz,
                            NULLIF(d.created_at,'')::timestamptz))
             - min(COALESCE(NULLIF(d.published_at,'')::timestamptz,
                            NULLIF(d.created_at,'')::timestamptz))
         )) > 4*3600
       ORDER BY m.cluster_id
    """
    if limit > 0:
        sql += " LIMIT ?"
        return connection.execute(sql, (limit,)).fetchall()
    return connection.execute(sql).fetchall()


def load_members(connection, cluster_id):
    return connection.execute(
        """SELECT d.id,d.source_id,d.source_kind,d.source_title,d.source_url,
                  d.source_text,d.full_text,d.content_hash,d.published_at,d.created_at,
                  m.match_type,m.similarity,m.propagation_role,
                  COALESCE(NULLIF(d.published_at,'')::timestamptz,
                           NULLIF(d.created_at,'')::timestamptz) AS event_at
             FROM primary_info_cluster_members_v1 m
             JOIN source_documents d ON d.id=m.source_document_id
            WHERE m.cluster_id=?
              AND m.match_type IN ('canonical','exact')
            ORDER BY event_at,d.id""",
        (cluster_id,),
    ).fetchall()


def create_cluster(connection, seed_row):
    row = connection.execute(
        """INSERT INTO primary_info_clusters_v1(
               canonical_document_id,classifier_version,first_seen_at,last_seen_at,
               spread_count,unique_sources)
           VALUES(?,?,?, ?,1,1)
           RETURNING id""",
        (
            int(seed_row["id"]),
            VERSION,
            seed_row["event_at"],
            seed_row["event_at"],
        ),
    ).fetchone()
    return int(row["id"])


def apply_split(connection, cluster_id, groups):
    cluster = connection.execute(
        "SELECT canonical_document_id FROM primary_info_clusters_v1 WHERE id=?",
        (cluster_id,),
    ).fetchone()
    if not cluster:
        return []

    current_canonical = int(cluster["canonical_document_id"])
    keep_index = 0
    for index, group in enumerate(groups):
        if any(int(row["id"]) == current_canonical for row in group):
            keep_index = index
            break

    affected = [cluster_id]
    for index, group in enumerate(groups):
        if index == keep_index:
            target_cluster = cluster_id
            seed_id = current_canonical
        else:
            seed = group[0]
            target_cluster = create_cluster(connection, seed)
            affected.append(target_cluster)
            seed_id = int(seed["id"])

        member_ids = [int(row["id"]) for row in group]
        connection.execute(
            """UPDATE primary_info_cluster_members_v1
                  SET cluster_id=?,
                      match_type=CASE WHEN source_document_id=? THEN 'canonical' ELSE 'exact' END,
                      similarity=1.0,
                      propagation_role=CASE WHEN source_document_id=? THEN 'origin' ELSE 'repeater' END
                WHERE source_document_id = ANY(?)""",
            (target_cluster, seed_id, seed_id, member_ids),
        )
        connection.execute(
            """UPDATE primary_info_queue_v1
                  SET cluster_id=?,canonical_document_id=?,updated_at=now()
                WHERE source_document_id = ANY(?)""",
            (target_cluster, seed_id, member_ids),
        )
        connection.execute(
            """UPDATE info_prefilter_results_v1
                  SET cluster_id=?,canonical_document_id=?,updated_at=now()
                WHERE source_document_id = ANY(?)""",
            (target_cluster, seed_id, member_ids),
        )

    for target_cluster in affected:
        update_cluster_and_prefilter(connection, target_cluster)
    return affected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    connection = db.connect(readonly=False)
    try:
        candidates = load_candidates(connection, args.limit)
        split_clusters = 0
        new_clusters = 0
        moved_members = 0
        examples = []

        for item in candidates:
            cluster_id = int(item["cluster_id"])
            members = load_members(connection, cluster_id)
            groups = split_groups(members)
            if len(groups) <= 1:
                continue
            split_clusters += 1
            new_clusters += len(groups) - 1
            moved_members += sum(len(group) for group in groups)
            if len(examples) < 20:
                examples.append({
                    "cluster_id": cluster_id,
                    "groups": [
                        {
                            "n": len(group),
                            "first": str(group[0]["event_at"]),
                            "last": str(group[-1]["event_at"]),
                            "sources": sorted({str(row["source_title"]) for row in group}),
                            "text": (group[0]["source_text"] or "")[:120],
                        }
                        for group in groups
                    ],
                })

            if args.apply:
                apply_split(connection, cluster_id, groups)
                connection.commit()

        print({
            "candidates": len(candidates),
            "split_clusters": split_clusters,
            "new_clusters": new_clusters,
            "members_in_split_clusters": moved_members,
            "apply": args.apply,
        })
        for example in examples:
            print(example)
    finally:
        connection.close()


if __name__ == "__main__":
    main()

