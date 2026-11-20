"""Idempotent upgrade for an already restored ParserPon4ik PostgreSQL DB."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db


def main():
    connection = db.connect()
    try:
        connection.raw.execute((db.ROOT / "database" / "extensions.sql").read_text(encoding="utf-8"))
        connection.raw.execute((db.ROOT / "database" / "post_import.sql").read_text(encoding="utf-8"))
        connection.commit()
        print("Схему трьох профілів і календар подій встановлено.")
        for table in ("note_drafts", "event_candidates", "calendar_events", "analysis_jobs"):
            print(table, connection.execute("SELECT count(*) FROM " + table).fetchone()[0])
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()
