"""Convert already completed Markdown research results to Word."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db, report_docx


def main():
    connection = db.connect()
    converted = 0
    try:
        rows = connection.execute(
            "SELECT id,prompt,window_start,window_end,output_path FROM analysis_jobs WHERE status='completed' AND output_path ILIKE '%.md'"
        ).fetchall()
        for row in rows:
            source = Path(row["output_path"])
            if not source.is_absolute():
                source = db.ROOT / source
            if not source.is_file():
                continue
            target = source.with_suffix(".docx")
            report_docx.markdown_to_docx(
                source.read_text(encoding="utf-8"), target,
                row["prompt"] or "Тематичне дослідження",
                f"Період: {row['window_start']} — {row['window_end']}",
            )
            connection.execute("UPDATE analysis_jobs SET output_path=? WHERE id=?", (str(target), row["id"]))
            converted += 1
        connection.commit()
    finally:
        connection.close()
    print(f"Аналітичних результатів переведено у Word: {converted}")


if __name__ == "__main__":
    main()
