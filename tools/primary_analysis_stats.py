"""Print and persist current primary-analysis catch-up statistics."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import primary_stats


def main() -> int:
    try:
        stats = primary_stats.collect()
        path = primary_stats.save_snapshot(stats)
    except Exception as error:
        print(f"ПОМИЛКА читання статистики: {error}")
        return 1
    print(primary_stats.render_text(stats))
    print(f"\nJSON: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
