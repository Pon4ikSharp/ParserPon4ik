from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import dzen_matcher

LOG = ROOT / "runtime" / "logs" / "dzen_matcher.log"
LOOKBACK_HOURS = max(2, int(os.getenv("DZEN_MATCH_LOOKBACK_HOURS", "12")))


def main() -> None:
    started = time.monotonic()
    payload = {"at": datetime.now(timezone.utc).isoformat(), "status": "ok"}
    try:
        payload.update(dzen_matcher.build_matches(commit=True, lookback_hours=LOOKBACK_HOURS))
        payload.update(dzen_matcher.refresh_source_candidates())
    except Exception as exc:
        payload.update({"status": "error", "error": str(exc)[:1600]})
        raise
    finally:
        payload["elapsed_seconds"] = round(time.monotonic() - started, 3)
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        print(json.dumps(payload, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
