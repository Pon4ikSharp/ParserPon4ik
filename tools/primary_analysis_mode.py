"""Enable/disable the temporary collection + primary-analysis mode."""
from __future__ import annotations
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import process_manager, primary_stats


def main(action: str) -> int:
    if action == "start":
        result = process_manager.enable_primary_only()
        if not result["running"]:
            print("ПОМИЛКА: collector не запустився. Перевірте runtime\\logs\\collector.log")
            return 1
        print("Увімкнено: збір + первинний аналіз.")
        print(f"Collector PID: {result['pid']}")
        print("BR/editor/analyst і розсилка не запускаються.")
        print("Первинний аналіз: 4 паралельні worker-и (1 LIVE + 3 BACKLOG, адаптивна допомога LIVE), safe RSS canonical-URL dedup + in-flight exact/near dedup.")
        print("Черга обробляється від 05.09.2026 00:00 за Києвом; вже оброблені матеріали пропускаються.")
        return 0
    if action == "stop":
        process_manager.disable_primary_only()
        print("Тимчасовий режим вимкнено; collector зупинено. Інші воркери автоматично не запускалися.")
        return 0
    status = process_manager.status("collector")
    print("Режим: " + ("увімкнено" if process_manager.primary_only_enabled() else "вимкнено"))
    print("Collector: " + (f"працює PID {status['pid']}" if status["running"] else "зупинений"))
    if process_manager.primary_only_enabled():
        try:
            stats = primary_stats.collect()
            primary_stats.save_snapshot(stats)
            print()
            print(primary_stats.render_text(stats))
        except Exception as error:
            print(f"Не вдалося прочитати статистику: {error}")
    return 0

if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("action", choices=("start","stop","status"))
    raise SystemExit(main(parser.parse_args().action))
