"""One on-demand queue for daily, weekly, research and calendar jobs."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from app import articles, codex_runtime, db, editor, report_docx, telemetry, workflow

ROOT = db.ROOT
KYIV = ZoneInfo("Europe/Kyiv")
OUTPUTS = ROOT / "analysis_reports"


class MaterialsPending(RuntimeError):
    def __init__(self, count):
        self.count = int(count)
        super().__init__(
            f"Очікує первинного відбору: {self.count} матеріалів. "
            "Збирач автоматично опрацьовує цю чергу"
        )


def local_time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KYIV)
    return parsed.astimezone(timezone.utc)


def claim_job(connection):
    row = connection.execute(
        """SELECT * FROM analysis_jobs WHERE status='queued'
           AND (started_at IS NULL OR started_at<now()-interval '60 seconds')
           ORDER BY priority,created_at,id FOR UPDATE SKIP LOCKED LIMIT 1"""
    ).fetchone()
    if not row:
        connection.commit()
        return None
    connection.execute(
        "UPDATE analysis_jobs SET status='running',started_at=now(),error='' WHERE id=?",
        (row["id"],),
    )
    connection.commit()
    return row


def finish(connection, job_id, status, output_path="", result=None, error=""):
    connection.execute(
        """UPDATE analysis_jobs SET status=?,output_path=?,result=?::jsonb,error=?,
           completed_at=now() WHERE id=?""",
        (status, output_path, json.dumps(result or {}, ensure_ascii=False), error[:4000], job_id),
    )
    connection.commit()


def defer(connection, job_id, error):
    connection.execute(
        """UPDATE analysis_jobs SET status='queued',output_path='',result='{}'::jsonb,
           error=?,started_at=now(),completed_at=NULL WHERE id=?""", (str(error)[:4000], job_id)
    )
    connection.commit()


def run_command(arguments):
    completed = subprocess.run(
        [sys.executable, *arguments], cwd=ROOT, text=True, encoding="utf-8",
        capture_output=True, env=os.environ.copy()
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr[-4000:] or completed.stdout[-4000:])
    return completed.stdout


def word_path_from_output(output):
    for raw in str(output or "").splitlines():
        value = raw.strip().strip('"')
        if not value.lower().endswith(".docx"):
            continue
        path = Path(value)
        if not path.is_absolute():
            path = ROOT / path
        if path.is_file():
            return path.resolve()
    raise RuntimeError("Аналітичний модуль завершився, але не повідомив шлях до Word-файлу")


def codex_text(prompt, web=False, model=None, stage="analysis"):
    timer = telemetry.started()
    executable = codex_runtime.executable("analyst")
    selected_model = model or codex_runtime.model("analyst")
    command = [executable]
    if web:
        command.append("--search")
    command += ["exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
                "--model", selected_model, "-"]
    completed = subprocess.run(
        command, input=prompt, cwd=ROOT, text=True, encoding="utf-8", capture_output=True,
        timeout=max(300, int(os.getenv("ANALYST_CODEX_TIMEOUT", "1800"))),
        env=codex_runtime.environment("analyst"),
    )
    if completed.returncode:
        error = codex_runtime.command_error("analyst", completed)
        telemetry.record("analyst", stage, selected_model, prompt, completed.stdout, timer, error)
        raise RuntimeError(error)
    telemetry.record("analyst", stage, selected_model, prompt, completed.stdout, timer)
    return completed.stdout.strip()


def research(connection, job, options, folder):
    start = job["window_start"] or datetime(1970, 1, 1, tzinfo=timezone.utc)
    end = job["window_end"] or datetime.now(timezone.utc)
    rows = connection.execute(
        """SELECT d.id,d.published_at,d.source_title,d.source_url,
                  COALESCE(NULLIF(d.full_text,''),d.source_text) AS source_text,
                  d.content_status,s.groups,s.audiences,i.status AS influence_status,
                  i.narrative_title,i.summary,i.claims,i.techniques,i.actors
           FROM source_documents d JOIN sources s ON s.id=d.source_id
           JOIN info_influence i ON i.source_document_id=d.id
           WHERE s.for_info AND i.status='accepted'
             AND NULLIF(d.published_at,'')::timestamptz>=?
             AND NULLIF(d.published_at,'')::timestamptz<?
             AND d.content_status IN ('telegram','extracted','manual')
           ORDER BY NULLIF(d.published_at,'')::timestamptz,d.id""",
        (start, end),
    ).fetchall()
    evidence = folder / "evidence.jsonl"
    with evidence.open("w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(dict(row), ensure_ascii=False, default=str) + "\n")
    packages, current, size = [], [], 0
    for row in rows:
        item = dict(row)
        item_size = len(item["source_text"] or "")
        if current and size + item_size > 70000:
            packages.append(current); current=[]; size=0
        current.append(item); size += item_size
    if current:
        packages.append(current)
    findings = []
    for index, package in enumerate(packages, 1):
        payload = [{"id": r["id"], "date": r["published_at"], "source": r["source_title"],
                    "url": r["source_url"], "text": r["source_text"],
                    "influence": r.get("influence_status"), "narrative": r.get("narrative_title")}
                   for r in package]
        findings.append(codex_text(
            f"""Проаналізуй пакет оригіналів для запиту: {job['prompt']}
Використовуй лише матеріали пакета. Поверни стислий перелік релевантних фактів,
заяв, дат і механізмів впливу. Кожне твердження познач [DBO:id]. Не переказуй
нерелевантне і не роби фінального висновку.
<MATERIALS>{json.dumps(payload,ensure_ascii=False,default=str)}</MATERIALS>""",
            stage="research_package",
        ))
    final = codex_text(
        f"""Підготуй відповідь українською на аналітичний запит: {job['prompt']}
Період: {start} — {end}. Спирайся на пакетні знахідки, зберігай посилання
[DBO:id]. Відокрем факти з БД від додаткових вебвідомостей. Не вигадуй даних.
Якщо вебпошук доступний і ввімкнений, доповни лише прогалини та додай прямі URL.
<FINDINGS>{json.dumps(findings,ensure_ascii=False)}</FINDINGS>""",
        web=bool(options.get("web")),
        model=codex_runtime.escalation_model() if options.get("enhanced") else None,
        stage="research_final_enhanced" if options.get("enhanced") else "research_final",
    )
    markdown_output = folder / "result.md"
    markdown_output.write_text(final, encoding="utf-8")
    word_output = folder / "result.docx"
    report_docx.markdown_to_docx(
        final, word_output,
        job["prompt"] or "Тематичне дослідження",
        f"Період: {start.astimezone(KYIV):%d.%m.%Y %H:%M} — {end.astimezone(KYIV):%d.%m.%Y %H:%M}",
    )
    return word_output, {
        "source_count": len(rows), "package_count": len(packages),
        "web": bool(options.get("web")), "markdown_path": str(markdown_output),
    }


def calendar_export(connection, job, folder):
    start = job["window_start"] or datetime(1970, 1, 1, tzinfo=timezone.utc)
    end = job["window_end"] or datetime(2100, 1, 1, tzinfo=timezone.utc)
    rows = connection.execute(
        """SELECT e.*,
          (SELECT jsonb_agg(jsonb_build_object('url',s.source_url,'document_id',s.source_document_id))
           FROM calendar_event_sources s WHERE s.event_id=e.id) AS sources
          FROM calendar_events e
          WHERE (e.starts_at>=? AND e.starts_at<?) OR e.starts_at IS NULL
          ORDER BY e.starts_at NULLS LAST,e.id""", (start, end)
    ).fetchall()
    raw_output = folder / "calendar.json"
    raw_output.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    output = folder / "calendar.docx"
    report_docx.calendar_to_docx(
        [dict(row) for row in rows], output, "Календар подій",
        f"Період: {start.astimezone(KYIV):%d.%m.%Y} — {end.astimezone(KYIV):%d.%m.%Y}",
    )
    return output, {"event_count": len(rows), "json_path": str(raw_output)}


def process_job(connection, job):
    options = job["options"] if isinstance(job["options"], dict) else json.loads(job["options"])
    folder = OUTPUTS / f"job-{job['id']}"
    folder.mkdir(parents=True, exist_ok=True)
    if job["job_type"] == "research":
        return research(connection, job, options, folder)
    if job["job_type"] == "calendar":
        return calendar_export(connection, job, folder)
    pending = articles.pending_classification_count(
        connection, job["window_start"], job["window_end"]
    )
    if pending:
        raise MaterialsPending(pending)
    start = job["window_start"].astimezone(KYIV).strftime("%Y-%m-%dT%H:%M")
    end = job["window_end"].astimezone(KYIV).strftime("%Y-%m-%dT%H:%M")
    if job["job_type"] == "daily":
        output = run_command(["daily_infospace.py", "--start", start, "--end", end])
    else:
        command = ["weekly_infospace.py", "create", "--name", options.get("name", f"weekly-{job['id']}"),
                   "--start", start, "--end", end]
        for flag, key in (("--manual-dir", "manual_dir"), ("--briefing-dir", "briefing_dir")):
            if options.get(key): command += [flag, options[key]]
        output = run_command(command)
    log = folder / "run.log"
    log.write_text(output, encoding="utf-8")
    word_output = word_path_from_output(output)
    return word_output, {"command_completed": True, "log_path": str(log)}


def claim_translation(connection):
    row=connection.execute(
        """SELECT * FROM document_translations WHERE status='queued'
           AND (started_at IS NULL OR started_at<now()-interval '60 seconds')
           ORDER BY created_at,source_document_id FOR UPDATE SKIP LOCKED LIMIT 1"""
    ).fetchone()
    if not row:
        connection.commit(); return None
    connection.execute(
        """UPDATE document_translations SET status='processing',attempts=attempts+1,
           started_at=now(),error='',updated_at=now() WHERE source_document_id=?""",
        (row["source_document_id"],),
    )
    connection.commit()
    return row


def process_translation(connection, task):
    document=connection.execute(
        "SELECT COALESCE(NULLIF(full_text,''),source_text) AS text FROM source_documents WHERE id=?",
        (task["source_document_id"],),
    ).fetchone()
    if not document or not (document["text"] or "").strip():
        raise RuntimeError("Оригінал не містить тексту для перекладу")
    translated=[]
    for index, chunk in enumerate(articles.segment(document["text"], 45000), 1):
        translated.append(codex_text(
            f"""Переклади наведений фрагмент українською мовою повністю й точно.
Не скорочуй, не аналізуй і не додавай пояснень. Збережи абзаци, прямі цитати,
власні назви, числа, дати, хештеги та послідовність тексту. Поверни лише переклад.
Фрагмент {index}.
<SOURCE>{chunk}</SOURCE>""",
            model=codex_runtime.model("analyst"), stage="translation",
        ))
    result="\n\n".join(translated).strip()
    connection.execute(
        """UPDATE document_translations SET status='completed',translated_text=?,
           model=?,error='',completed_at=now(),updated_at=now() WHERE source_document_id=?""",
        (result,codex_runtime.model("analyst"),task["source_document_id"]),
    )
    connection.commit()


def finish_translation_error(connection, task, error):
    transient=codex_runtime.is_transient_error(error)
    connection.execute(
        """UPDATE document_translations SET status=?,error=?,started_at=now(),
           completed_at=CASE WHEN ? THEN NULL ELSE now() END,updated_at=now()
           WHERE source_document_id=?""",
        ("queued" if transient else "failed",str(error)[:4000],transient,task["source_document_id"]),
    )
    connection.commit()


def run_worker(once=False):
    load_dotenv(ROOT / ".env")
    editor.write_schemas()
    connection = db.connect()
    try:
        while True:
            job = claim_job(connection)
            if not job:
                event_worked=asyncio.run(editor.process_event(connection,"analyst"))
                if event_worked:
                    if once: return
                    continue
                if once: return
                time.sleep(max(2, int(os.getenv("ANALYST_POLL_SECONDS", "5"))))
                continue
            try:
                output, result = process_job(connection, job)
                finish(connection, job["id"], "completed", str(output), result)
                print(f"Завдання {job['id']} завершено: {output}")
            except MaterialsPending as error:
                defer(connection, job["id"], error)
                print(f"Завдання {job['id']} відкладено: {error}")
                if once: return
                time.sleep(5)
                continue
            except Exception as error:
                if codex_runtime.is_transient_error(error):
                    defer(connection,job["id"],error)
                    print(f"Завдання {job['id']} відкладено через тимчасову недоступність моделі: {error}")
                else:
                    finish(connection, job["id"], "failed", error=str(error))
                    print(f"Завдання {job['id']} завершилося помилкою: {error}")
            if once: return
    finally:
        connection.close()


def cli():
    parser = argparse.ArgumentParser(description="Черга аналітика")
    sub = parser.add_subparsers(dest="command", required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("job_type", choices=["daily", "weekly", "research", "calendar"])
    submit.add_argument("--start"); submit.add_argument("--end"); submit.add_argument("--prompt", default="")
    submit.add_argument("--web", action="store_true"); submit.add_argument("--enhanced", action="store_true")
    submit.add_argument("--name", default="")
    submit.add_argument("--manual-dir"); submit.add_argument("--briefing-dir"); submit.add_argument("--priority", type=int, default=100)
    worker = sub.add_parser("worker"); worker.add_argument("--once", action="store_true")
    sub.add_parser("list")
    args = parser.parse_args(); load_dotenv(ROOT / ".env")
    if args.command == "worker": return run_worker(args.once)
    connection = db.connect()
    try:
        if args.command == "list":
            for row in connection.execute("SELECT id,job_type,status,created_at,output_path,error FROM analysis_jobs ORDER BY id DESC LIMIT 50"):
                print(dict(row))
            return
        if not (args.start and args.end):
            parser.error("Потрібні межі періоду: --start і --end")
        if args.job_type == "research" and not args.prompt.strip():
            parser.error("Для дослідження потрібен --prompt")
        job_id = workflow.submit_analysis_job(
            connection, args.job_type, args.prompt,
            local_time(args.start) if args.start else None, local_time(args.end) if args.end else None,
            {"web": args.web, "enhanced": args.enhanced, "name": args.name,
             "manual_dir": args.manual_dir, "briefing_dir": args.briefing_dir},
            args.priority,
        )
        print("Завдання поставлено в чергу:", job_id)
    finally:
        connection.close()


if __name__ == "__main__":
    cli()
