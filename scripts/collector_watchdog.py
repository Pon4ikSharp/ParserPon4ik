from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import process_manager

RUNTIME = ROOT / "runtime"
HEARTBEAT = RUNTIME / "parser_heartbeat.json"
MAINTENANCE_STOP = RUNTIME / "maintenance.stop"


def parse_time(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def heartbeat_age_minutes():
    if not HEARTBEAT.exists():
        return None
    try:
        payload = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    stamp = parse_time(payload.get("heartbeat_at"))
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds() / 60.0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stale-minutes", type=float, default=30.0)
    args = parser.parse_args()

    now = datetime.now(timezone.utc).isoformat()
    if MAINTENANCE_STOP.exists():
        print(json.dumps({
            "at": now,
            "action": "skip",
            "reason": "maintenance_stop",
        }), flush=True)
        return

    status = process_manager.status("collector")
    age = heartbeat_age_minutes()

    if not status["running"]:
        started = process_manager.start("collector")
        print(json.dumps({
            "at": now,
            "action": "start",
            "reason": "process_missing",
            "heartbeat_age_minutes": age,
            "pid": started.get("pid"),
        }), flush=True)
        return

    if age is not None and age > max(10.0, args.stale_minutes):
        old_pid = status.get("pid")
        process_manager.stop("collector")
        # stop() intentionally writes maintenance.stop; clear it because this
        # restart is watchdog-driven rather than an operator maintenance stop.
        MAINTENANCE_STOP.unlink(missing_ok=True)
        started = process_manager.start("collector")
        print(json.dumps({
            "at": now,
            "action": "restart",
            "reason": "heartbeat_stale",
            "heartbeat_age_minutes": round(age, 2),
            "old_pid": old_pid,
            "pid": started.get("pid"),
        }), flush=True)
        return

    print(json.dumps({
        "at": now,
        "action": "ok",
        "pid": status.get("pid"),
        "heartbeat_age_minutes": None if age is None else round(age, 2),
    }), flush=True)


if __name__ == "__main__":
    main()

