"""Read-only queue status for urgent operator diagnostics."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db


def main():
    connection = db.connect(readonly=True)
    try:
        print("ЧЕРГА РЕДАКТОРА")
        for row in connection.execute(
            """SELECT status,count(*) AS total,min(created_at) AS oldest,
                      max(created_at) AS newest
                 FROM note_drafts
                WHERE status IN ('queued','processing','manual_review','failed')
                GROUP BY status ORDER BY status"""
        ):
            print(dict(row))
        active = connection.execute(
            """SELECT id,status,attempts,created_at,locked_at,left(error,500) AS error
                 FROM note_drafts
                WHERE status IN ('processing','manual_review','failed')
                ORDER BY CASE status WHEN 'processing' THEN 0 ELSE 1 END,
                         created_at,id LIMIT 20"""
        ).fetchall()
        if active:
            print("\nАКТИВНІ АБО ПРОБЛЕМНІ ЗАПИСИ")
            for row in active:
                print(dict(row))

        print("\nЧЕРГА АНАЛІТИКА ПОДІЙ")
        for row in connection.execute(
            """SELECT status,count(*) AS total,min(created_at) AS oldest,
                      max(created_at) AS newest
                 FROM event_candidates
                WHERE status IN ('queued','processing','manual_review','failed')
                GROUP BY status ORDER BY status"""
        ):
            print(dict(row))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
