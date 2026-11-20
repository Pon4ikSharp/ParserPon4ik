"""Safely start or stop ingestion without any AI workers."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import process_manager


def main(action: str) -> int:
    if action == "start":
        result = process_manager.enable_primary_only()
        if not result["running"]:
            print("ÐŸÐžÐœÐ˜Ð›ÐšÐ: Ð¿Ð°Ñ€ÑÐµÑ€ Ð½Ðµ Ð·Ð°Ð¿ÑƒÑÑ‚Ð¸Ð²ÑÑ. ÐŸÐµÑ€ÐµÐ²Ñ–Ñ€Ñ‚Ðµ Ð¶ÑƒÑ€Ð½Ð°Ð» Ð·Ð±Ð¸Ñ€Ð°Ñ‡Ð°.")
            return 1
        print("Ð ÐµÐ¶Ð¸Ð¼ Ð»Ð¸ÑˆÐµ Ð·Ð±Ð¸Ñ€Ð°Ð½Ð½Ñ ÑƒÐ²Ñ–Ð¼ÐºÐ½ÐµÐ½Ð¾.")
        print(f"PID Ð¿Ð°Ñ€ÑÐµÑ€Ð°: {result['pid']}")
        print("Ð ÐµÐ´Ð°ÐºÑ‚Ð¾Ñ€, Ð°Ð½Ð°Ð»Ñ–Ñ‚Ð¸Ðº Ñ– Ð²Ð¸ÐºÐ»Ð¸ÐºÐ¸ Codex Ð²Ð¸Ð¼ÐºÐ½ÐµÐ½Ñ–.")
        print("ÐžÑ€Ð¸Ð³Ñ–Ð½Ð°Ð»Ð¸ Telegram Ñ‚Ð° RSS Ð½Ð°ÐºÐ¾Ð¿Ð¸Ñ‡ÑƒÑŽÑ‚ÑŒÑÑ Ñƒ Ð½Ð°ÑÐ²Ð½Ñ–Ð¹ Ð‘Ð”.")
        return 0

    if action == "stop":
        process_manager.disable_primary_only()
        print("ÐŸÐ°Ñ€ÑÐµÑ€ Ð·ÑƒÐ¿Ð¸Ð½ÐµÐ½Ð¾. Ð ÐµÐ¶Ð¸Ð¼ Ð»Ð¸ÑˆÐµ Ð·Ð±Ð¸Ñ€Ð°Ð½Ð½Ñ Ð²Ð¸Ð¼ÐºÐ½ÐµÐ½Ð¾.")
        print("Ð—Ð²Ð¸Ñ‡Ð°Ð¹Ð½Ñ– Ð¿Ñ€Ð¾Ñ†ÐµÑÐ¸ ÑÐ°Ð¼Ð¾ÑÑ‚Ñ–Ð¹Ð½Ð¾ Ð½Ðµ Ð·Ð°Ð¿ÑƒÑÐºÐ°Ð»Ð¸ÑÑ.")
        return 0

    result = process_manager.status("collector")
    print(
        "Ð ÐµÐ¶Ð¸Ð¼ Ð»Ð¸ÑˆÐµ Ð·Ð±Ð¸Ñ€Ð°Ð½Ð½Ñ: "
        + ("ÑƒÐ²Ñ–Ð¼ÐºÐ½ÐµÐ½Ð¾" if process_manager.primary_only_enabled() else "Ð²Ð¸Ð¼ÐºÐ½ÐµÐ½Ð¾")
    )
    print("ÐŸÐ°Ñ€ÑÐµÑ€: " + (f"Ð¿Ñ€Ð°Ñ†ÑŽÑ”, PID {result['pid']}" if result["running"] else "Ð·ÑƒÐ¿Ð¸Ð½ÐµÐ½Ð¸Ð¹"))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop", "status"))
    args = parser.parse_args()
    raise SystemExit(main(args.action))
