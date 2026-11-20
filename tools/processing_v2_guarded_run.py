"""Guarded orchestration for bounded Processing Engine v2 AI batches.

Dry-run by default. Real execution requires both --apply and an explicit
--max-used-percent threshold. Uses only the Collector profile through the
existing bounded benchmark worker.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import ROOT, connect
from app import processing_v2

USAGE_TOOL = ROOT / "tools" / "processing_v2_collector_usage.ps1"
AI_WORKER = ROOT / "tools" / "processing_v2_ai_benchmark.py"

PERCENT_RE = re.compile(r"\blimit=codex\s+primary_used=(\d+)\b")
ALLOWED_RE = re.compile(r"\bordinaryUsageAllowed=(True|False)\b", re.I)


def usage_snapshot() -> tuple[int, bool, str]:
    powershell = shutil.which("powershell") or shutil.which("powershell.exe")
    if not powershell:
        raise RuntimeError("PowerShell was not found")
    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(USAGE_TOOL),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
        cwd=ROOT,
    )
    output = (completed.stdout or "") + "\n" + (completed.stderr or "")
    if completed.returncode != 0:
        raise RuntimeError(
            "Collector usage snapshot failed: " + output.strip()[-1200:]
        )
    percent_match = PERCENT_RE.search(output)
    allowed_match = ALLOWED_RE.search(output)
    if not percent_match:
        raise RuntimeError("Collector usage percent was not returned")
    used_percent = int(percent_match.group(1))
    ordinary_allowed = (
        allowed_match.group(1).lower() == "true"
        if allowed_match else True
    )
    return used_percent, ordinary_allowed, output.strip()


def ready_analysis_jobs() -> int:
    connection = connect(readonly=True)
    try:
        row = connection.execute(
            """SELECT count(*) AS n
                 FROM processing_v2_jobs
                WHERE pipeline_version=?
                  AND stage='analyze'
                  AND status IN ('pending','retry')
                  AND next_attempt_at<=now()""",
            (processing_v2.PIPELINE_VERSION,),
        ).fetchone()
        return int(row["n"] or 0)
    finally:
        connection.close()


def run_one_batch(*, batch_size: int, reasoning: str, verbosity: str) -> None:
    command = [
        sys.executable,
        str(AI_WORKER),
        "--apply",
        "--max-docs",
        str(batch_size),
        "--reasoning",
        reasoning,
        "--verbosity",
        verbosity,
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"bounded AI worker exited with code {completed.returncode}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--max-batches", type=int, default=1)
    parser.add_argument("--max-used-percent", type=int)
    parser.add_argument("--reasoning", default="low")
    parser.add_argument("--verbosity", default="low")
    args = parser.parse_args()

    batch_size = max(1, min(int(args.batch_size), 20))
    max_batches = max(1, min(int(args.max_batches), 100))

    used, allowed, _ = usage_snapshot()
    ready = ready_analysis_jobs()

    print("Processing Engine v2 — GUARDED AI RUN")
    print("Profile: collector only")
    print(f"Current 7-day Codex usage: {used}%")
    print(f"Ordinary usage allowed: {allowed}")
    print(f"Ready analysis jobs: {ready}")
    print(f"Batch size: {batch_size}")
    print(f"Max batches: {max_batches}")

    if not args.apply:
        print("AI calls: 0")
        print("DB writes: 0")
        print(
            "Real execution requires --apply and an explicit "
            "--max-used-percent value."
        )
        return 0

    if args.max_used_percent is None:
        raise SystemExit(
            "--max-used-percent is required with --apply; "
            "no implicit allowance threshold is used"
        )

    threshold = int(args.max_used_percent)
    if threshold < 1 or threshold > 100:
        raise SystemExit("--max-used-percent must be between 1 and 100")

    if not allowed:
        print("STOP: ordinary Collector usage is not allowed.")
        return 2
    if used >= threshold:
        print(
            f"STOP: current usage {used}% is already at/above "
            f"the configured {threshold}% threshold."
        )
        return 0

    completed_batches = 0
    for _ in range(max_batches):
        ready = ready_analysis_jobs()
        if ready <= 0:
            print("STOP: no ready analysis jobs.")
            break

        used_before, allowed_before, _ = usage_snapshot()
        print(
            f"Pre-batch guard: usage={used_before}% "
            f"allowed={allowed_before} ready={ready}"
        )
        if not allowed_before:
            print("STOP: ordinary Collector usage is no longer allowed.")
            break
        if used_before >= threshold:
            print(
                f"STOP: usage {used_before}% reached configured "
                f"{threshold}% threshold."
            )
            break

        run_one_batch(
            batch_size=min(batch_size, ready),
            reasoning=args.reasoning,
            verbosity=args.verbosity,
        )
        completed_batches += 1

        used_after, allowed_after, _ = usage_snapshot()
        print(
            f"Post-batch guard: usage={used_after}% "
            f"allowed={allowed_after}"
        )
        if not allowed_after:
            print("STOP: ordinary Collector usage was disabled.")
            break
        if used_after >= threshold:
            print(
                f"STOP: usage {used_after}% reached configured "
                f"{threshold}% threshold."
            )
            break

    print(f"Completed batches: {completed_batches}")
    print(f"Remaining ready jobs: {ready_analysis_jobs()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
