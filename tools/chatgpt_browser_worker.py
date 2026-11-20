from __future__ import annotations
import argparse
import json
import os
import sys
import traceback
from pathlib import Path

from chatgpt_browser_use import ChatGPT

DEFAULT_PROFILE = r"C:\ParserPon4ik_PostgreSQL\chatgpt-browser-use\chrome-profile"
DEFAULT_CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--output-file", required=True)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--new-chat", action="store_true")
    args = ap.parse_args()

    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    profile = os.getenv("SOFT_CHATGPT_PROFILE", DEFAULT_PROFILE)
    chrome = os.getenv("SOFT_CHATGPT_CHROME", DEFAULT_CHROME)
    bot = ChatGPT(user_data_dir=profile, chromium_bin=chrome, cdp_port=9222)
    result = {"ok": False, "reply": "", "url": "", "error": ""}
    try:
        bot.start()
        if args.new_chat:
            # The library exposes conversation reset via CLI/server, but for
            # direct Python use we keep the current chat unless explicitly
            # extended later with a stable public API.
            pass
        reply = bot.send(prompt, timeout=args.timeout)
        result["reply"] = (reply or "").strip()
        result["url"] = bot.url() or ""
        if not result["reply"]:
            raise RuntimeError("ChatGPT returned an empty response")
        result["ok"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
    finally:
        try:
            bot.stop()
        except Exception:
            pass
    Path(args.output_file).write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0 if result["ok"] else 2

if __name__ == "__main__":
    raise SystemExit(main())
