"""Best-effort local accounting for Codex calls (no prompt contents stored)."""
from __future__ import annotations

import time

from app import db


def started() -> float:
    return time.monotonic()


def record(role, stage, model, prompt, output="", start=None, error=""):
    """Record only sizes/timing; telemetry must never interrupt collection."""
    connection = None
    try:
        connection = db.connect()
        connection.execute(
            """INSERT INTO model_usage(role,stage,model,input_chars,output_chars,
                 duration_ms,success,error) VALUES (?,?,?,?,?,?,?,?)""",
            (
                role,
                stage,
                model,
                len(prompt or ""),
                len(output or ""),
                int((time.monotonic() - start) * 1000) if start is not None else 0,
                not bool(error),
                str(error)[:2000],
            ),
        )
        connection.commit()
    except Exception:
        if connection:
            connection.rollback()
    finally:
        if connection:
            connection.close()
