"""Show real local Codex call volume without exposing prompts."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db


connection = db.connect(readonly=True)
try:
    rows = connection.execute(
        """SELECT role,model,count(*) AS calls,
                  round(sum(input_chars)/1000000.0,3) AS input_mchars,
                  round(sum(output_chars)/1000000.0,3) AS output_mchars,
                  round(avg(duration_ms)/1000.0,1) AS avg_seconds,
                  count(*) FILTER (WHERE NOT success) AS errors
           FROM model_usage
           GROUP BY role,model ORDER BY role,model"""
    ).fetchall()
    if not rows:
        print("Викликів моделей ще не зафіксовано.")
    for row in rows:
        print(dict(row))
finally:
    connection.close()
