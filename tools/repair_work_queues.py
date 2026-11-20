"""One-time safe repair when queue ownership changes to the analyst profile."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db


def main():
    connection=db.connect()
    try:
        legacy_info=connection.execute(
            """UPDATE info_influence
               SET status=CASE
                     WHEN length(trim(COALESCE(narrative_title,'')))>0
                       OR role IN ('carrier','amplifier','reporting','trigger')
                       THEN 'accepted'
                     ELSE 'rejected'
                   END,
                   reason=CASE WHEN length(trim(COALESCE(reason,'')))>0 THEN reason
                               ELSE 'Відновлено статус наявного результату первинного відбору' END,
                   updated_at=COALESCE(updated_at,now()::text)
               WHERE length(trim(COALESCE(status,'')))=0
               RETURNING id"""
        ).fetchall()
        events=connection.execute(
            """UPDATE event_candidates SET status='queued',attempts=0,error='',
               locked_at=NULL,completed_at=NULL
               WHERE status IN ('processing','manual_review','failed') RETURNING id"""
        ).fetchall()
        notes=connection.execute(
            """UPDATE note_drafts SET status='queued',attempts=0,error='',
               locked_at=NULL,completed_at=NULL
               WHERE status='processing' OR (
                 status IN ('manual_review','failed') AND (
                   lower(error) LIKE '%codex%' OR lower(error) LIKE '%capacity%' OR
                   lower(error) LIKE '%rate limit%' OR lower(error) LIKE '%network%' OR
                   lower(error) LIKE '%bad gateway%')) RETURNING id"""
        ).fetchall()
        analysis=connection.execute(
            """UPDATE analysis_jobs SET status='queued',started_at=NULL,completed_at=NULL,
               output_path='',result='{}'::jsonb
               WHERE status='running' OR (status='failed' AND (
                 lower(error) LIKE '%capacity%' OR lower(error) LIKE '%rate limit%' OR
                 lower(error) LIKE '%bad gateway%' OR lower(error) LIKE '%temporarily%'))
               RETURNING id"""
        ).fetchall()
        connection.commit()
        print(f"Відновлено статусів первинного відбору: {len(legacy_info)}")
        print(f"Події повернуто аналітику: {len(events)}")
        print(f"Технічні чернетки повернуто редактору: {len(notes)}")
        print(f"Тимчасово невдалі аналітичні завдання повернуто: {len(analysis)}")
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()
