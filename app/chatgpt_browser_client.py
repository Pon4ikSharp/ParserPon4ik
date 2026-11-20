from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BROWSER_ROOT = Path(r"C:\ParserPon4ik_PostgreSQL\chatgpt-browser-use")
PYTHON = BROWSER_ROOT / ".venv" / "Scripts" / "python.exe"
WORKER = ROOT / "tools" / "chatgpt_browser_worker.py"
RUNTIME = ROOT / "runtime" / "chatgpt_browser"
LOCK_FILE = RUNTIME / "send.lock"
LOG_FILE = RUNTIME / "client.log"

logger = logging.getLogger("soft.chatgpt_browser")

def _ensure_runtime() -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)

def _configure_logging() -> None:
    _ensure_runtime()
    if logger.handlers:
        return
    handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
@contextmanager
def _process_lock(wait_seconds: int = 600):
    _ensure_runtime()
    import msvcrt

    fh = open(LOCK_FILE, "a+b")
    deadline = time.monotonic() + wait_seconds
    acquired = False
    try:
        while time.monotonic() < deadline:
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
                break
            except OSError:
                time.sleep(0.5)
        if not acquired:
            raise TimeoutError("Timed out waiting for ChatGPT browser lock")
        yield
    finally:
        if acquired:
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        fh.close()

class ChatGPTBrowserClient:
    def __init__(self, timeout: int = 300, retries: int = 2):
        self.timeout = timeout
        self.retries = retries
        _configure_logging()
    def _validate(self) -> None:
        missing = [str(p) for p in (PYTHON, WORKER) if not p.exists()]
        if missing:
            raise FileNotFoundError("Missing ChatGPT browser components: " + ", ".join(missing))

    def send(self, prompt: str, *, new_chat: bool = False) -> str:
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("prompt must not be empty")
        self._validate()

        with _process_lock(wait_seconds=max(self.timeout + 120, 600)):
            last_error: Exception | None = None
            for attempt in range(1, self.retries + 2):
                try:
                    reply = self._send_once(prompt, new_chat=new_chat)
                    if reply.strip():
                        return reply
                    raise RuntimeError("empty response")
                except Exception as exc:
                    last_error = exc
                    logger.warning("attempt=%s failed: %s", attempt, exc)
                    if attempt <= self.retries:
                        time.sleep(min(2 * attempt, 5))
            assert last_error is not None
            raise last_error

    def _send_once(self, prompt: str, *, new_chat: bool) -> str:
        _ensure_runtime()
        stamp = f"{os.getpid()}_{int(time.time() * 1000)}"
        prompt_file = RUNTIME / f"prompt_{stamp}.txt"
        output_file = RUNTIME / f"result_{stamp}.json"
        prompt_file.write_text(prompt, encoding="utf-8")
        cmd = [
            str(PYTHON),
            str(WORKER),
            "--prompt-file", str(prompt_file),
            "--output-file", str(output_file),
            "--timeout", str(self.timeout),
        ]
        if new_chat:
            cmd.append("--new-chat")

        logger.info("send start chars=%s", len(prompt))
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(BROWSER_ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout + 150,
                check=False,
            )
            if not output_file.exists():
                raise RuntimeError(
                    f"worker produced no result rc={proc.returncode} stderr={proc.stderr[-1000:]}"
                )
            data = json.loads(output_file.read_text(encoding="utf-8"))
            if not data.get("ok"):
                raise RuntimeError(data.get("error") or f"worker rc={proc.returncode}")
            reply = str(data.get("reply") or "").strip()
            logger.info("send ok chars=%s url=%s", len(reply), data.get("url", ""))
            return reply
        finally:
            for p in (prompt_file, output_file):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass

def send(prompt: str, *, timeout: int = 300, retries: int = 2, new_chat: bool = False) -> str:
    return ChatGPTBrowserClient(timeout=timeout, retries=retries).send(
        prompt,
        new_chat=new_chat,
    )
