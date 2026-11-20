"""Start and stop ParserPon4ik local workers without exposing a network API."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import psutil
except ImportError:  # Patch must also work before optional dependencies are installed.
    psutil = None

from app.db import ROOT

RUNTIME_DIR = ROOT / "runtime"
LOG_DIR = RUNTIME_DIR / "logs"
STATE_PATH = RUNTIME_DIR / "processes.json"
CLEAR_STATE_PATH = RUNTIME_DIR / "log_views.json"
PRIMARY_ONLY_FLAG = RUNTIME_DIR / "primary_analysis_only.flag"
LOCK = threading.RLock()

SPECS = {
    # During phase 0/1 the collector is intentionally the raw-only parser.
    # INFO/BR analysis will be attached as a separate controlled stage later.
    "collector": [sys.executable, str(ROOT / "collect_raw_only.py")],
    "editor": [sys.executable, str(ROOT / "editor.py")],
    "analyst": [sys.executable, str(ROOT / "analyst.py"), "worker"],
}


def primary_only_enabled():
    return PRIMARY_ONLY_FLAG.is_file()


def child_environment(role=None):
    environment = os.environ.copy()
    # Windows servers may otherwise select cp1252/cp437 for redirected output
    # and crash as soon as a Ukrainian log line is printed.
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    if role == "collector" and primary_only_enabled():
        environment["PRIMARY_ANALYSIS_ONLY"] = "1"
        environment.setdefault("PRIMARY_BACKLOG_SINCE", "2026-09-04T21:00:00+00:00")
        environment.setdefault("PRIMARY_SCREEN_WORKERS", "2")
        environment.setdefault("PRIMARY_FULL_WORKERS", "8")
        environment.setdefault("PRIMARY_LIVE_WINDOW_MINUTES", "30")
        environment.setdefault("PRIMARY_WORKER_DELAY", "0.4")
        environment.setdefault("PRIMARY_RATE_BACKOFF", "20")
        environment.setdefault("PRIMARY_TRANSIENT_MAX_ATTEMPTS", "2")
        environment.setdefault("PRIMARY_NEAR_DUP_THRESHOLD", "0.86")
        environment.setdefault("PRIMARY_NEAR_DUP_WINDOW_HOURS", "36")
        environment.setdefault("PRIMARY_LIVE_HELP_THRESHOLDS", "100,250,400")
    else:
        environment.pop("PRIMARY_ANALYSIS_ONLY", None)
    return environment


def _ensure_dirs():
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def _load():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(state):
    _ensure_dirs()
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_PATH)


def _fallback_cmdline(pid):
    """Return a process command line without requiring psutil."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    if os.name == "nt":
        # PowerShell is present on supported Windows Server/Windows installs.
        script = (
            f"$p = Get-CimInstance Win32_Process -Filter \"ProcessId = {pid}\" "
            "-ErrorAction SilentlyContinue; if ($p) { $p.CommandLine }"
        )
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=5, check=False,
            )
            return (completed.stdout or "").strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        return proc_cmdline.read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def _matches(role, pid):
    expected = Path(SPECS[role][1]).name.lower()
    if psutil is None:
        command_line = _fallback_cmdline(pid).lower()
        return bool(command_line and expected in command_line)
    try:
        process = psutil.Process(int(pid))
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return False
        return any(Path(part).name.lower() == expected for part in process.cmdline())
    except (psutil.Error, OSError, ValueError):
        return False


def _find_existing(role):
    """Adopt an already-running managed script after a panel/server restart."""
    if psutil is None:
        return None
    expected = Path(SPECS[role][1]).name.lower()
    try:
        for process in psutil.process_iter(["pid", "cmdline", "create_time"]):
            try:
                cmdline = process.info.get("cmdline") or []
                if any(Path(str(part)).name.lower() == expected for part in cmdline):
                    return process
            except (psutil.Error, OSError, ValueError):
                continue
    except psutil.Error:
        return None
    return None


def status(role):
    if role not in SPECS:
        raise ValueError(role)
    with LOCK:
        state = _load()
        item = state.get(role, {})
        running = bool(item.get("pid") and _matches(role, item["pid"]))
        if not running:
            adopted = _find_existing(role)
            if adopted is not None:
                item = {
                    **item,
                    "pid": adopted.pid,
                    "started_at": datetime.fromtimestamp(
                        adopted.create_time(), tz=timezone.utc
                    ).isoformat(),
                    "stopped_at": "",
                }
                state[role] = item
                _save(state)
                running = True
        if item and not running:
            item["stopped_at"] = item.get("stopped_at") or datetime.now(timezone.utc).isoformat()
            item["pid"] = None
            state[role] = item
            _save(state)
        return {
            "role": role,
            "running": running,
            "pid": item.get("pid") if running else None,
            "started_at": item.get("started_at", ""),
            "stopped_at": item.get("stopped_at", ""),
            "log": str(LOG_DIR / f"{role}.log"),
        }


def all_statuses():
    return {role: status(role) for role in SPECS}


def start(role):
    if role not in SPECS:
        raise ValueError(role)
    with LOCK:
        if primary_only_enabled() and role != "collector":
            raise RuntimeError("Увімкнено режим збору + первинного аналізу. Editor і Analyst вимкнені.")
        current = status(role)
        if current["running"]:
            return current
        _ensure_dirs()
        if role == "collector":
            (RUNTIME_DIR / "maintenance.stop").unlink(missing_ok=True)
        log = (LOG_DIR / f"{role}.log").open("a", encoding="utf-8", buffering=1)
        log.write(f"\n[{datetime.now(timezone.utc).isoformat()}] Запуск {role}\n")
        kwargs = {
            "cwd": ROOT,
            "stdin": subprocess.DEVNULL,
            "stdout": log,
            "stderr": subprocess.STDOUT,
            "env": child_environment(role),
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(SPECS[role], **kwargs)
        state = _load()
        state[role] = {
            "pid": process.pid,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "stopped_at": "",
        }
        _save(state)
        log.close()
        time.sleep(0.15)
        return status(role)


def _signal_gracefully(role, pid):
    if os.name == "nt":
        os.kill(pid, signal.CTRL_BREAK_EVENT)
    else:
        os.killpg(os.getpgid(pid), signal.SIGINT)


def _process_tree(pid):
    """Capture descendants before the parent exits and loses their ownership."""
    if psutil is None:
        try:
            return int(pid), []
        except (TypeError, ValueError):
            return None, []
    try:
        parent = psutil.Process(pid)
        return parent, parent.children(recursive=True)
    except psutil.Error:
        return None, []


def _stop_survivors(processes, timeout=5):
    if psutil is None:
        pids = []
        for process in processes:
            if process is None:
                continue
            try:
                pids.append(int(process))
            except (TypeError, ValueError):
                pass
        for pid in reversed(list(dict.fromkeys(pids))):
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=max(5, timeout), check=False,
                    )
                else:
                    os.kill(pid, signal.SIGTERM)
            except (OSError, subprocess.SubprocessError):
                pass
        return

    survivors = []
    for process in reversed(processes):
        try:
            if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                process.terminate()
                survivors.append(process)
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(survivors, timeout=timeout)
    for process in alive:
        try:
            process.kill()
        except psutil.Error:
            pass
    if alive:
        psutil.wait_procs(alive, timeout=timeout)


def stop(role, timeout=25):
    if role not in SPECS:
        raise ValueError(role)
    with LOCK:
        current = status(role)
        if not current["running"]:
            return current
        pid = current["pid"]
        parent, descendants = _process_tree(pid)
        if role == "collector":
            _ensure_dirs()
            (RUNTIME_DIR / "maintenance.stop").write_text(
                datetime.now(timezone.utc).isoformat(), encoding="utf-8"
            )
        else:
            try:
                _signal_gracefully(role, pid)
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and _matches(role, pid):
            time.sleep(0.25)
        # A Python worker may exit before its Codex child.  Stop the captured
        # descendants even when the recorded parent is already gone; otherwise
        # codex.exe keeps profile SQLite/WAL files locked on Windows.
        _stop_survivors(descendants + ([parent] if parent is not None else []))
        state = _load()
        state[role] = {
            **state.get(role, {}),
            "pid": None,
            "stopped_at": datetime.now(timezone.utc).isoformat(),
        }
        _save(state)
        return status(role)


def start_all():
    if primary_only_enabled():
        return {
            "collector": start("collector"),
            "editor": status("editor"),
            "analyst": status("analyst"),
        }
    # The editor must be ready before new drafts begin arriving.
    return {role: start(role) for role in ("editor", "analyst", "collector")}


def stop_all():
    # Collector goes first so its clean shutdown can send the maintenance GIF.
    return {role: stop(role) for role in ("collector", "editor", "analyst")}


def enable_primary_only():
    """Stop downstream workers and run only ingestion + primary info classification."""
    stop_all()
    _ensure_dirs()
    PRIMARY_ONLY_FLAG.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    return start("collector")


def disable_primary_only():
    """Stop collector and restore normal worker start behaviour."""
    stop("collector")
    PRIMARY_ONLY_FLAG.unlink(missing_ok=True)
    return all_statuses()


def tail(role, lines=160):
    if role not in SPECS:
        raise ValueError(role)
    path = LOG_DIR / f"{role}.log"
    if not path.is_file():
        return "Журнал ще порожній."
    try:
        offsets = json.loads(CLEAR_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        offsets = {}
    with path.open("r", encoding="utf-8", errors="replace") as source:
        offset = min(int(offsets.get(role, 0)), path.stat().st_size)
        source.seek(offset)
        content = source.readlines()
    return "".join(content[-max(10, min(lines, 1000)):])


def clear_view(role):
    if role not in SPECS:
        raise ValueError(role)
    _ensure_dirs()
    try:
        offsets = json.loads(CLEAR_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        offsets = {}
    path = LOG_DIR / f"{role}.log"
    offsets[role] = path.stat().st_size if path.is_file() else 0
    CLEAR_STATE_PATH.write_text(json.dumps(offsets, indent=2), encoding="utf-8")


def raw_tail(role, lines=300):
    if role not in SPECS:
        raise ValueError(role)
    path = LOG_DIR / f"{role}.log"
    if not path.is_file():
        return ""
    with path.open("r", encoding="utf-8", errors="replace") as source:
        return "".join(source.readlines()[-max(10, min(lines, 2000)):])
