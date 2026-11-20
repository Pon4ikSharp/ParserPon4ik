import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import hold_v2, story_propagation

hold = hold_v2.sync_now()
propagation = story_propagation.sync_now()
print(json.dumps({
    "at": datetime.now(timezone.utc).isoformat(),
    "hold_v2_version": hold_v2.VERSION,
    "hold_v2": hold,
    "propagation": propagation,
}, ensure_ascii=False, default=str))
