"""Bounded AI benchmark worker for Processing Engine v2.

Uses only the Collector ChatGPT/Codex profile. Editor and Analyst profiles are
never referenced. Claims a bounded batch of pending analysis jobs, sends one
compact structured request, stores per-document results in processing_v2_* and
records aggregate usage metadata without storing prompts.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from app import codex_runtime, processing_v2
from app.db import ROOT, connect

SCHEMA_PATH = ROOT / "processing_v2_screen_schema.json"
PROMPT_VERSION = "processing-v2-screen-v2"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING = "low"
DEFAULT_VERBOSITY = "low"


def _codex_executable() -> str:
    try:
        return codex_runtime.executable("collector")
    except RuntimeError:
        pattern = (
            ROOT
            / "runtime"
            / "_codex_npx_cache"
            / "_npx"
        )
        matches = list(
            pattern.glob(
                "*/node_modules/@openai/codex-win32-x64/"
                "vendor/x86_64-pc-windows-msvc/bin/codex.exe"
            )
        )
        if matches:
            return str(matches[0])
        raise


def _choose_text(row) -> tuple[str, str]:
    hydrated = (row["hydrated_text"] or "").strip()
    hydration_status = (row["hydration_status"] or "").strip().lower()
    if hydrated:
        if hydration_status == "extracted":
            return "hydration_v2", hydrated
        return "hydration_v2_partial", hydrated
    full_text = (row["full_text"] or "").strip()
    if full_text:
        return "full_text", full_text
    source_text = (row["source_text"] or "").strip()
    if source_text:
        return "source_text", source_text
    return "feed_text", (row["feed_text"] or "").strip()


def _bounded_text(text: str, maximum: int = 6000) -> str:
    text = (text or "").strip()
    if len(text) <= maximum:
        return text
    head = text[: maximum // 2]
    tail = text[-(maximum // 2):]
    return head + "\n[...TRUNCATED FOR SCREENING...]\n" + tail


def _build_prompt(items: list[dict]) -> str:
    payload = [
        {
            "id": item["id"],
            "source_kind": item["source_kind"],
            "source_title": item["source_title"],
            "text": _bounded_text(item["text"]),
        }
        for item in items
    ]
    instructions = """
You are a compact screening classifier for a Ukrainian security/news monitoring system.
Analyze ONLY the supplied documents. Do not browse, use tools, read files, or add facts
that are not in the text. Return exactly one result for every input id and no extra ids.

BRIEFING relevance:
- relevant when the material has meaningful value for monitoring Ukraine's national
  interests, foreign policy, defence/security, military-technical issues, strategic
  economy/energy, cyber, information sphere, occupied territories, crisis regions,
  significant international decisions or developments.
- routine INTERNAL-UKRAINE material is normally NOT briefing-relevant: local strikes
  and casualty-only reports, ordinary domestic incidents, routine local politics or
  administration should be false unless there is a clear strategic or international
  dimension.
- internal-Ukraine exceptions that may be relevant include: military exercises or
  training, introduction/testing of new weapons or capabilities, collegium/board-level
  decisions, inspections, new forms/methods of employment, major nationwide strategic
  decisions, or a material foreign/international dimension.
- occupied territories remain a separate relevant monitoring area.
- ordinary lifestyle, sport, celebrity, local crime/accidents and non-strategic noise
  should be false unless there is a clear national-security/international relevance.

INFLUENCE relevance:
- relevant when the material contains, promotes, repeats or materially exposes a
  potentially manipulative/propagandistic thesis or narrative relevant to information
  influence against Ukraine, Ukrainian institutions, Western support for Ukraine,
  legitimacy, morale, mobilisation, sovereignty, territorial issues or allied policy.
- a neutral factual mention of Russia/Ukraine alone is not enough.

EVENT relevance:
- relevant for a concrete significant meeting, visit, summit, exercise, election,
  official decision, deadline, scheduled negotiation, deployment or comparable
  event that can be clustered or placed on a calendar.

Scoring:
- 0-39: weak/no relevance
- 40-69: uncertain/context dependent
- 70-100: strong relevance
Set relevant=true normally at score >=70. If evidence is incomplete, lower confidence
and set needs_review=true.

Keep the response compact:
- summary: one short Ukrainian sentence, factual, <=220 characters
- entities: up to 6 important people/organizations/countries
- topics: up to 4 short Ukrainian topic labels
- reason: one short Ukrainian rationale, <=180 characters
- language: ISO-like short code when obvious (uk, ru, en, pl, hu, sk, ar, etc.)
""".strip()
    return (
        instructions
        + "\n\nINPUT_DOCUMENTS_JSON:\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _extract_usage(jsonl: str) -> dict[str, int | None]:
    found: dict[str, int | None] = {
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
    }

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in found and isinstance(child, int):
                    current = found[key]
                    if current is None or child > current:
                        found[key] = child
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for line in jsonl.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            visit(json.loads(line))
        except json.JSONDecodeError:
            continue
    return found


def _validate_batch(payload: dict, expected_ids: set[int]) -> list[dict]:
    if not isinstance(payload, dict):
        raise ValueError("model output is not an object")
    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError("model output has no results array")
    actual_ids: list[int] = []
    for result in results:
        if not isinstance(result, dict):
            raise ValueError("result item is not an object")
        raw_id = result.get("id")
        if not isinstance(raw_id, int):
            raise ValueError("result id is not an integer")
        actual_ids.append(raw_id)
    if len(actual_ids) != len(set(actual_ids)):
        raise ValueError("model output contains duplicate ids")
    if set(actual_ids) != expected_ids:
        missing = sorted(expected_ids - set(actual_ids))
        extra = sorted(set(actual_ids) - expected_ids)
        raise ValueError(f"id mismatch; missing={missing}, extra={extra}")
    return results


def _load_claimed_documents(connection, jobs) -> list[dict]:
    items = []
    for job in jobs:
        row = connection.execute(
            """SELECT d.id,d.source_kind,d.source_title,d.source_url,
                      d.source_text,d.feed_text,d.full_text,d.content_hash,
                      h.status AS hydration_status,
                      h.full_text AS hydrated_text
                 FROM source_documents d
                 LEFT JOIN processing_v2_hydration_results h
                   ON h.source_document_id=d.id
                  AND h.pipeline_version=?
                  AND h.source_hash=?
                WHERE d.id=?""",
            (
                str(job["pipeline_version"]),
                str(job["source_hash"]),
                int(job["source_document_id"]),
            ),
        ).fetchone()
        if not row:
            raise ValueError(
                f"source document {job['source_document_id']} disappeared"
            )
        basis, text = _choose_text(row)
        if not text:
            raise ValueError(f"source document {row['id']} has no usable text")
        items.append(
            {
                "id": int(row["id"]),
                "source_kind": str(row["source_kind"]),
                "source_title": str(row["source_title"] or ""),
                "basis": basis,
                "text": text,
                "job": job,
            }
        )
    return items


def _run_model(items: list[dict], *, model: str,
               reasoning: str, verbosity: str) -> tuple[dict, dict, int, int, str]:
    executable = _codex_executable()
    prompt = _build_prompt(items)
    started = time.monotonic()
    env = codex_runtime.environment("collector")

    with tempfile.TemporaryDirectory(
        prefix="processing-v2-screen-", dir=str(ROOT / "runtime")
    ) as temp_dir:
        output_path = Path(temp_dir) / "result.json"
        command = [
            executable,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--ignore-user-config",
            "--model",
            model,
            "-c",
            f'model_reasoning_effort="{reasoning}"',
            "-c",
            f'model_verbosity="{verbosity}"',
            "--output-schema",
            str(SCHEMA_PATH),
            "--json",
            "--output-last-message",
            str(output_path),
            "-",
        ]
        completed = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=temp_dir,
            env=env,
            timeout=300,
            check=False,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        jsonl = completed.stdout or ""
        usage = _extract_usage(jsonl)

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(
                "Collector model call failed: " + detail[-1500:]
            )
        if not output_path.exists():
            raise RuntimeError("Collector model call produced no final output file")

        output_text = output_path.read_text(encoding="utf-8").strip()
        if not output_text:
            raise RuntimeError("Collector model returned empty output")
        payload = json.loads(output_text)
        return payload, usage, duration_ms, len(prompt), output_text


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually make one bounded Collector AI call and persist v2 results.",
    )
    parser.add_argument("--max-docs", type=int, default=20)
    parser.add_argument("--model", default="")
    parser.add_argument("--reasoning", default=DEFAULT_REASONING)
    parser.add_argument("--verbosity", default=DEFAULT_VERBOSITY)
    args = parser.parse_args()

    max_docs = max(1, min(int(args.max_docs), 20))
    model = args.model.strip() or codex_runtime.model("collector") or DEFAULT_MODEL
    reasoning = args.reasoning.strip() or DEFAULT_REASONING
    verbosity = args.verbosity.strip() or DEFAULT_VERBOSITY

    connection = connect(readonly=not args.apply)
    jobs = []
    call_meta: dict | None = None
    try:
        ready = connection.execute(
            """SELECT count(*) AS n
                 FROM processing_v2_jobs
                WHERE pipeline_version=?
                  AND stage='analyze'
                  AND status IN ('pending','retry')
                  AND next_attempt_at<=now()""",
            (processing_v2.PIPELINE_VERSION,),
        ).fetchone()
        print("Processing Engine v2 — AI SCREEN benchmark")
        print("Profile: collector only")
        print(f"Model: {model}")
        print(f"Reasoning: {reasoning}")
        print(f"Verbosity: {verbosity}")
        print(f"Ready analysis jobs: {int(ready['n'] or 0)}")
        print(f"Hard max docs: {max_docs}")

        if not args.apply:
            print("AI calls: 0")
            print("DB writes: 0")
            print("Add --apply to run exactly one bounded batch.")
            return 0

        jobs = processing_v2.claim_batch(
            connection,
            "processing-v2-ai-screen",
            stage="analyze",
            limit=max_docs,
        )
        if not jobs:
            print("No ready analysis jobs.")
            return 0

        items = _load_claimed_documents(connection, jobs)
        expected_ids = {item["id"] for item in items}
        payload, usage, duration_ms, input_chars, output_text = _run_model(
            items,
            model=model,
            reasoning=reasoning,
            verbosity=verbosity,
        )
        call_meta = {
            "document_count": len(items),
            "input_chars": input_chars,
            "output_chars": len(output_text),
            "input_tokens": usage["input_tokens"],
            "cached_input_tokens": usage["cached_input_tokens"],
            "output_tokens": usage["output_tokens"],
            "duration_ms": duration_ms,
        }
        results = _validate_batch(payload, expected_ids)
        by_id = {int(result["id"]): result for result in results}

        per_doc_ms = duration_ms // max(1, len(items))
        for item in items:
            result = by_id[item["id"]]
            processing_v2.store_analysis(
                connection,
                source_document_id=item["id"],
                pipeline_version=processing_v2.PIPELINE_VERSION,
                source_hash=str(item["job"]["source_hash"]),
                input_basis=item["basis"],
                input_chars=len(item["text"]),
                result=result,
                model=model,
                prompt_version=PROMPT_VERSION,
                duration_ms=per_doc_ms,
                commit=False,
            )
            processing_v2.complete_job(
                connection,
                int(item["job"]["id"]),
                worker="processing-v2-ai-screen",
                detail={
                    "prompt_version": PROMPT_VERSION,
                    "briefing_score": result["briefing"]["score"],
                    "influence_score": result["influence"]["score"],
                    "event_score": result["event"]["score"],
                },
                commit=False,
            )

        processing_v2.record_ai_call(
            connection,
            pipeline_version=processing_v2.PIPELINE_VERSION,
            stage="screen",
            model=model,
            reasoning_effort=reasoning,
            verbosity=verbosity,
            document_count=len(items),
            input_chars=input_chars,
            output_chars=len(output_text),
            input_tokens=usage["input_tokens"],
            cached_input_tokens=usage["cached_input_tokens"],
            output_tokens=usage["output_tokens"],
            duration_ms=duration_ms,
            success=True,
            commit=False,
        )
        connection.commit()

        briefing_yes = sum(bool(r["briefing"]["relevant"]) for r in results)
        influence_yes = sum(bool(r["influence"]["relevant"]) for r in results)
        event_yes = sum(bool(r["event"]["relevant"]) for r in results)

        print()
        print("AI SCREEN RESULT")
        print(f"Documents: {len(items)}")
        print(f"Duration ms: {duration_ms}")
        print(f"Input chars: {input_chars}")
        print(f"Output chars: {len(output_text)}")
        print(f"Input tokens: {usage['input_tokens']}")
        print(f"Cached input tokens: {usage['cached_input_tokens']}")
        print(f"Output tokens: {usage['output_tokens']}")
        print(f"Briefing relevant: {briefing_yes}")
        print(f"Influence relevant: {influence_yes}")
        print(f"Event relevant: {event_yes}")
        return 0

    except Exception as error:
        if args.apply and jobs:
            try:
                connection.rollback()
                for job in jobs:
                    processing_v2.fail_job(
                        connection,
                        int(job["id"]),
                        str(error),
                        worker="processing-v2-ai-screen",
                        retry_delay_seconds=300,
                        commit=False,
                    )
                processing_v2.record_ai_call(
                    connection,
                    pipeline_version=processing_v2.PIPELINE_VERSION,
                    stage="screen",
                    model=model,
                    reasoning_effort=reasoning,
                    verbosity=verbosity,
                    document_count=(
                        int(call_meta["document_count"])
                        if call_meta else len(jobs)
                    ),
                    input_chars=(
                        int(call_meta["input_chars"]) if call_meta else 0
                    ),
                    output_chars=(
                        int(call_meta["output_chars"]) if call_meta else 0
                    ),
                    input_tokens=(
                        call_meta["input_tokens"] if call_meta else None
                    ),
                    cached_input_tokens=(
                        call_meta["cached_input_tokens"] if call_meta else None
                    ),
                    output_tokens=(
                        call_meta["output_tokens"] if call_meta else None
                    ),
                    duration_ms=(
                        int(call_meta["duration_ms"]) if call_meta else 0
                    ),
                    success=False,
                    error=str(error),
                    commit=False,
                )
                connection.commit()
            except Exception:
                connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
