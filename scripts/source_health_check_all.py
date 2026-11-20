import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db, source_admin

STATE = ROOT / "runtime" / "source_health_check_state.json"
LOG = ROOT / "runtime" / "logs" / "source_health_check.log"


def write_state(**payload):
    data = {"at": datetime.now(timezone.utc).isoformat(), **payload}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def log(message):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now(timezone.utc).isoformat()} {message}\n")
def main():
    started_at = datetime.now(timezone.utc).isoformat()
    connection = db.connect(readonly=False)
    try:
        rows = connection.execute("""
            SELECT e.endpoint,e.kind,e.source_id,s.title
              FROM source_endpoints e
              JOIN sources s ON s.id=e.source_id
             WHERE e.enabled AND s.enabled
             ORDER BY s.title,e.endpoint
        """).fetchall()
        total = len(rows)
        ok = failed = missing_total = 0
        write_state(
            status="running", pid=os.getpid(), started_at=started_at,
            total=total, done=0, ok=0, failed=0, missing_total=0,
            current="", last_error="",
        )
        for index, row in enumerate(rows, 1):
            endpoint = row["endpoint"]
            last_error = ""
            try:
                verified = source_admin.verify(endpoint)
                missing = source_admin.store_check(connection, endpoint, verified)
                missing_total += int(missing or 0)
                ok += 1
            except Exception as error:
                last_error = str(error)[:1000]
                failed += 1
                try:
                    source_admin.store_check(connection, endpoint, error=last_error)
                except Exception as store_error:
                    connection.rollback()
                    last_error += f" | store: {store_error}"
            state = write_state(
                status="running", pid=os.getpid(), started_at=started_at,
                total=total, done=index, ok=ok, failed=failed,
                missing_total=missing_total, current=endpoint,
                last_error=last_error,
            )
            if last_error:
                log(f"FAIL {endpoint} :: {last_error}")
            else:
                log(f"OK {endpoint}")
        write_state(
            status="completed", pid=os.getpid(), started_at=started_at,
            total=total, done=total, ok=ok, failed=failed,
            missing_total=missing_total, current="", last_error="",
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as error:
        connection.rollback()
        write_state(
            status="error", pid=os.getpid(), started_at=started_at,
            total=0, done=0, ok=0, failed=0, missing_total=0,
            current="", last_error=str(error)[:2000],
        )
        log(f"FATAL {error}")
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()
