"""Second-pass editor: validates drafts, maintains calendar, publishes Telegram."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Bot
from telegram.request import HTTPXRequest

from app import codex_runtime, collector, db, telemetry, workflow
from app.delivery import replace_news

ROOT = db.ROOT
KYIV = ZoneInfo("Europe/Kyiv")
NOTE_SCHEMA = ROOT / "codex_output_schema.json"
EVENT_SCHEMA = ROOT / "event_review_schema.json"


def editor_source_text(document):
    """Prefer the complete article while retaining Telegram-only originals."""
    return (
        document.get("full_text")
        or document.get("source_text")
        or document.get("feed_text")
        or ""
    )


def editor_date_context(document):
    value = document.get("published_at")
    if value:
        try:
            if isinstance(value, str):
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            published = value.astimezone(KYIV).strftime("%d.%m.%Y %H:%M")
        except (AttributeError, TypeError, ValueError):
            published = str(value)
    else:
        published = "невідома"
    return (
        f"Поточна дата за Києвом: {datetime.now(KYIV):%d.%m.%Y}.\n"
        f"Дата публікації матеріалу за Києвом: {published}."
    )


def document_is_too_old(document):
    """Never publish an archival backlog item as a fresh Telegram note."""
    value = document.get("published_at") or document.get("created_at")
    if not value:
        return False
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        maximum = max(1, int(os.getenv("RECOVERY_MAX_AGE_HOURS", "48")))
        return datetime.now(timezone.utc) - value.astimezone(timezone.utc) > timedelta(hours=maximum)
    except (AttributeError, TypeError, ValueError):
        return False


def editor_note_rules(document):
    return f"""ОБОВ'ЯЗКОВИЙ ШАБЛОН ЗАМІТКИ
- Не переписуй структуру на власний розсуд і не перетворюй замітку на переказ.
- paragraph починай коротким заголовком держав, міжнародних організацій або
  сторін події через дефіс і з крапкою: “Польща-НАТО-Україна.” або “ЄС-РФ.”.
  Одразу після заголовка продовжуй основний текст зв'язними реченнями.
- Перше речення основного тексту має містити точну дату події або заяви, якщо
  вона прямо встановлюється з оригіналу, головного учасника, при першій згадці
  його посаду, а також основну дію, рішення чи заяву.
- Не втрачай дати з оригіналу. Зберігай усі дати, потрібні для хронології:
  дату заяви, рішення, зустрічі, удару, набрання чинності, запланованої дії та
  попередньої події. “Сьогодні” перетворюй на дату публікації, “вчора” — на
  попередню календарну дату. Для поточного року не додавай рік. Якщо джерело
  не дає змоги встановити дату події, не вигадуй її.
- Розрізняй дату публікації і дату події. Якщо матеріал зараз повідомляє про
  давнішу подію, починай датою повідомлення та конструкцією “повідомив, що”,
  а дату самої події вкажи далі у відповідному місці.
- Після основного факту подай важливі уточнення, цифри, реакції, наслідки та
  застереження в логічній хронології. Не вилучай конкретні факти лише для
  скорочення тексту і не додавай загальних фраз для обсягу.
- Типовий обсяг — 500–1200 знаків. Один зв'язний абзац допустимий для короткої
  новини, складна подія може мати два абзаци. Не використовуй списки, Markdown,
  URL, емодзі або крапку з комою.
- Непідтверджені, російські та пропагандистські твердження подавай тільки з
  чіткою атрибуцією: “стверджується”, “за заявою”, “за версією”.
- Усіх осіб записуй як ініціал/ініціали та прізвище без пробілу. Під час першої
  згадки керівника держави, уряду або міністра обов'язково зазначай посаду.
- paragraph містить заголовок, основний текст і завершується джерелами в
  круглих дужках. reference_text використовуй лише для окремої передісторії й
  починай його словом “Довідково:”. Довідково — максимум два короткі речення
  лише з найважливішою передісторією; не дублюй основний текст і не додавай
  біографії, зайву статистику, прогнози або другорядні подробиці.
- event_key, facts_key та article_key описують повну замітку і залишаються
  стабільними для тієї самої події або статті.

КОНТРОЛЬ ПЕРЕД ВІДПОВІДДЮ
1. Чи збережено заголовок сторін через дефіс.
2. Чи проставлено кожну прямо відому й важливу дату та не переплутано її з
   датою публікації.
3. Чи є у першій згадці посадовця його посада та скорочене ім'я.
4. Чи збережено всі суттєві факти, цифри, реакції та застереження.
5. Чи paragraph є готовою самодостатньою заміткою, а не поясненням редактора.

{editor_date_context(document)}"""


def event_schema():
    properties = {
        "action": {"type": "string", "enum": ["new", "merge", "reject", "manual_review"]},
        "canonical_key": {"type": "string"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "starts_at": {"type": "string"},
        "ends_at": {"type": "string"},
        "all_day": {"type": "boolean"},
        "date_precision": {"type": "string", "enum": ["exact", "day", "month", "range", "approximate", "unknown"]},
        "status": {"type": "string", "enum": ["tentative", "confirmed", "postponed", "cancelled", "completed"]},
        "location": {"type": "string"},
        "participants": {"type": "array", "items": {"type": "string"}},
        "audiences": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "reason": {"type": "string"},
    }
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def write_schemas():
    collector.ensure_codex_schema()
    EVENT_SCHEMA.write_text(json.dumps(event_schema(), ensure_ascii=False, indent=2), encoding="utf-8")


def call_structured(prompt, schema_path, model, stage="review", web=False, runtime_role="editor"):
    timer = telemetry.started()
    executable = codex_runtime.executable(runtime_role)
    command = [executable]
    if web:
        command.append("--search")
    command += ["exec", "--ephemeral", "--skip-git-repo-check",
               "--sandbox", "read-only", "--model", model,
               "--output-schema", str(schema_path), "-"]
    result = subprocess.run(
        command, input=prompt, text=True, encoding="utf-8", capture_output=True,
        timeout=max(120, int(os.getenv(
            "ANALYST_CODEX_TIMEOUT" if runtime_role == "analyst" else "EDITOR_CODEX_TIMEOUT",
            "1800" if runtime_role == "analyst" else "600",
        ))),
        env=codex_runtime.environment(runtime_role), cwd=ROOT,
    )
    if result.returncode:
        error = codex_runtime.command_error(runtime_role, result)
        telemetry.record(runtime_role, stage, model, prompt, result.stdout, timer, error)
        raise RuntimeError(error)
    telemetry.record(runtime_role, stage, model, prompt, result.stdout, timer)
    return json.loads(result.stdout)


def note_prompt(draft, document, candidates):
    return f"""Ти другий незалежний редактор БР. Перевір чернетку першого Codex
за повним оригіналом. Не вигадуй фактів. Виправ фактичні, мовні та структурні
помилки. Перевір, чи це нова подія, оновлення або дублікат, враховуючи наявні
актуальні замітки нижче. Поля event_key, facts_key та article_key мають бути
стабільними. Якщо матеріал нерелевантний, relevant=false. Подію календаря
збережи в event_* полях лише якщо джерело справді містить анонс, заплановану
дату, перенесення, скасування або завершення заходу. Поверни JSON за схемою.

{editor_note_rules(document)}

ПОВНИЙ ОРИГІНАЛ
<SOURCE>{editor_source_text(document)}</SOURCE>

ЧЕРНЕТКА
<DRAFT>{json.dumps(draft['analysis'],ensure_ascii=False,default=str)}</DRAFT>

АКТУАЛЬНІ СХОЖІ ЗАМІТКИ
<CURRENT>{json.dumps(candidates,ensure_ascii=False,default=str)}</CURRENT>"""


def event_prompt(candidate, document, existing):
    return f"""Ти редактор календаря подій. Перевір кандидата виключно за
оригіналом. Визнач нову це подію чи уточнення наявної. Збережи точну дату і час
лише якщо вони прямо відомі. Для дати без часу використовуй 00:00:00+03:00,
all_day=true і precision=day. Якщо дата орієнтовна, precision=approximate і
status=tentative. Невідома дата: starts_at="", ends_at="", precision=unknown.
Подію, яка вже завершилась або дата якої минула, не додавай: action="reject".
Якщо вказані день і місяць без року, зістав їх із датою публікації та поточною
датою. Обирай лише майбутню дату; не перенось явно минулу подію на наступний рік.
Враховуй перенесення, скасування та завершення. canonical_key не повинен містити
дату, яка може змінитися. Пиши українською. Поверни JSON за схемою.

ОРИГІНАЛ
<SOURCE>{document.get('source_text','')}</SOURCE>

{editor_date_context(document)}

КАНДИДАТ
<CANDIDATE>{candidate['candidate_text']}</CANDIDATE>

МОЖЛИВА НАЯВНА ПОДІЯ
<EXISTING>{json.dumps(existing or {},ensure_ascii=False,default=str)}</EXISTING>"""


def event_enrichment_prompt(candidate, existing):
    return f"""Ти редактор календаря подій. Користувач вибірково передав на
уточнення вже наявну картку. Проведи вебпошук і доповни її перевіреними даними.

ОБОВ'ЯЗКОВО
- Знайди конкретних осіб замість узагальнених назв установ, якщо це можливо.
  Наприклад, “візит МЗС Ірану” треба уточнити до “візит міністра закордонних
  справ Ірану А.Арагчі”, лише якщо джерела підтверджують, що йдеться саме про
  цю особу на дату події.
- Перевір дату, час, місце, учасників, статус анонсу, перенесення або скасування.
- Надавай перевагу офіційним сайтам органів влади, міжнародних організацій і
  сторін заходу. Не вигадуй особу за самою назвою посади.
- Збережи всі вже підтверджені дані картки, якщо немає надійного спростування.
- title зроби конкретним і коротким, summary — самодостатнім. Осіб записуй як
  ініціал/ініціали та прізвище без пробілу.
- canonical_key не повинен містити дату, яка може змінитися. action="merge".
- Якщо надійно уточнити картку неможливо, action="manual_review" і поясни чому.
- Пиши українською та поверни лише JSON за схемою.

ПОТОЧНА КАРТКА
<EXISTING>{json.dumps(existing or {},ensure_ascii=False,default=str)}</EXISTING>

ЗАПИТ КОРИСТУВАЧА
<REQUEST>{candidate['candidate_text']}</REQUEST>"""


def current_notes(connection, target_id=None):
    rows = connection.execute(
        """SELECT id,category,paragraph,reference_text,event_key,facts_key,
                  article_key,primary_sources,source_links,created_at,updated_at
           FROM news WHERE is_current=1 ORDER BY updated_at DESC LIMIT 80"""
    ).fetchall()
    result = [dict(row) for row in rows]
    if target_id and not any(row["id"] == target_id for row in result):
        row = connection.execute("SELECT * FROM news WHERE id=?", (target_id,)).fetchone()
        if row:
            result.append(dict(row))
    return result


def record_decision(connection, draft_id, action, model, analysis, reason="",
                    target_news_id=None, result_news_id=None):
    connection.execute(
        """INSERT INTO editorial_decisions(
             draft_id,action,target_news_id,result_news_id,model,reviewed_analysis,reason)
           VALUES (?,?,?,?,?,?::jsonb,?)""",
        (draft_id, action, target_news_id, result_news_id, model,
         json.dumps(analysis, ensure_ascii=False), reason),
    )
    connection.commit()


def queue_failure_status(item, error, maximum_variable):
    """Bound retries so one broken item cannot monopolize a FIFO queue."""
    maximum = max(1, int(os.getenv(maximum_variable, "3")))
    attempt = int(item.get("attempts") or 0) + 1
    return ("queued" if attempt < maximum else "manual_review"), attempt, maximum


async def process_note(connection, application, allowed):
    draft = workflow.claim(connection, "note_drafts")
    if not draft:
        return False
    print(
        f"Редактор узяв чернетку #{draft['id']} "
        f"(спроба {int(draft.get('attempts') or 0) + 1})",
        flush=True,
    )
    try:
        document = connection.execute(
            "SELECT * FROM source_documents WHERE id=?", (draft["source_document_id"],)
        ).fetchone() or {}
        if document_is_too_old(document):
            maximum = max(1, int(os.getenv("RECOVERY_MAX_AGE_HOURS", "48")))
            reason = (
                f"Архівний матеріал старший за {maximum} годин; "
                "автоматичну Telegram-публікацію заблоковано"
            )
            workflow.finish(
                connection, "note_drafts", draft["id"], "rejected", reason
            )
            print(f"Чернетку #{draft['id']} не опубліковано: {reason}", flush=True)
            return True
        candidates = current_notes(connection, draft["target_news_id"])
        model = codex_runtime.model("editor")
        reviewed = call_structured(note_prompt(draft, document, candidates), NOTE_SCHEMA, model, "note_review")
        reviewed = collector.validate_codex_result(reviewed, draft["source_title"])
        if not reviewed["relevant"]:
            record_decision(connection, draft["id"], "reject", model, reviewed, reviewed["reason"])
            workflow.finish(connection, "note_drafts", draft["id"], "rejected")
            print(f"Чернетку #{draft['id']} відхилено редактором", flush=True)
            return True

        if reviewed["event_detected"]:
            workflow.enqueue_event_candidate(
                connection,
                source_kind=draft["source_kind"],
                source_chat_id=draft["source_chat_id"],
                source_message_id=draft["source_message_id"],
                candidate_key=reviewed["detected_event_key"],
                candidate_text=reviewed["event_text"],
                source_url=draft["source_url"],
                payload={"source_title": draft["source_title"], "analysis": reviewed},
                note_draft_id=draft["id"],
            )

        action, previous = collector.check_semantic_duplicate(
            connection, reviewed["event_key"], reviewed["facts_key"],
            reviewed["article_key"], reviewed["primary_sources"]
        )
        if action == "duplicate":
            record_decision(connection, draft["id"], action, model, reviewed,
                            f"Повтор замітки №{previous['id']}", previous["id"])
            workflow.finish(connection, "note_drafts", draft["id"], "duplicate")
            print(f"Чернетка #{draft['id']} є дублікатом", flush=True)
            return True

        if action == "update" and previous:
            prompt, title = collector.build_merge_prompt(previous, reviewed, draft["source_title"])
            prompt = f"{prompt}\n\n{editor_note_rules(document)}"
            reviewed = call_structured(prompt, NOTE_SCHEMA, model, "note_merge")
            reviewed = collector.validate_codex_result(reviewed, title)
            if not reviewed["relevant"] or not collector.facts_materially_changed(
                reviewed["facts_key"], previous["facts_key"]
            ):
                record_decision(connection, draft["id"], "duplicate", model, reviewed,
                                "Оновлення не містить нових фактів", previous["id"])
                workflow.finish(connection, "note_drafts", draft["id"], "duplicate")
                print(f"Чернетка #{draft['id']} не містить нових фактів", flush=True)
                return True

        previous_id = previous["id"] if previous else None
        news_id = collector.record_news(
            connection, draft["source_chat_id"], draft["source_message_id"],
            reviewed, draft["source_title"], draft["source_url"], previous=previous
        )
        links = collector.combined_source_links(previous, draft["source_title"], draft["source_url"])
        outgoing = collector.format_result(reviewed, action == "update", links)
        delivered, failed = await replace_news(
            application, connection, news_id, outgoing, allowed,
            [previous_id] if previous_id else []
        )
        record_decision(connection, draft["id"], action, model, reviewed,
                        f"Доставлено {delivered}, помилок {failed}", previous_id, news_id)
        workflow.finish(connection, "note_drafts", draft["id"], "published")
        print(
            f"Чернетку #{draft['id']} опубліковано: доставлено {delivered}, "
            f"помилок {failed}",
            flush=True,
        )
    except Exception as error:
        status, attempt, maximum = queue_failure_status(
            draft, error, "EDITOR_MAX_ATTEMPTS"
        )
        workflow.finish(connection, "note_drafts", draft["id"], status, str(error))
        print(
            f"Помилка чернетки #{draft['id']} (спроба {attempt}/{maximum}, "
            f"новий статус {status}): {type(error).__name__}: {error}",
            flush=True,
        )
        if status == "queued":
            await asyncio.sleep(max(2, int(os.getenv("EDITOR_RETRY_DELAY", "15"))))
    return True


def parse_time(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KYIV)
    return parsed.astimezone(timezone.utc).isoformat()


def event_is_past(reviewed, now=None):
    """True when a proposed new event has no remaining future calendar time."""
    now = now or datetime.now(timezone.utc)
    start = parse_time(reviewed.get("starts_at"))
    end = parse_time(reviewed.get("ends_at"))
    if not start:
        return False
    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end) if end else None
    if reviewed.get("all_day"):
        last = (end_dt or start_dt).astimezone(KYIV).date()
        return last < now.astimezone(KYIV).date()
    return (end_dt or start_dt) < now


def similar_event(connection, key):
    rows = connection.execute("SELECT * FROM calendar_events ORDER BY updated_at DESC LIMIT 500").fetchall()
    scored = [(collector.event_similarity(key, row["canonical_key"]), row) for row in rows]
    return max(scored, default=(0, None), key=lambda item: item[0]) if scored else (0, None)


def save_event(connection, candidate, reviewed, existing, model):
    if not existing and event_is_past(reviewed):
        return None
    now = datetime.now(timezone.utc).isoformat()
    values = (
        reviewed["canonical_key"], reviewed["title"], reviewed["summary"],
        parse_time(reviewed["starts_at"]), parse_time(reviewed["ends_at"]), reviewed["all_day"],
        reviewed["date_precision"], reviewed["status"], reviewed["location"],
        json.dumps(reviewed["participants"], ensure_ascii=False),
        json.dumps(reviewed["audiences"], ensure_ascii=False),
        json.dumps(reviewed["tags"], ensure_ascii=False), reviewed["confidence"], now,
    )
    if existing:
        event_id = existing["id"]
        before = dict(existing)
        connection.execute(
            """UPDATE calendar_events SET canonical_key=?,title=?,summary=?,starts_at=?,ends_at=?,
                 all_day=?,date_precision=?,status=?,location=?,participants=?::jsonb,audiences=?::jsonb,
                 tags=?::jsonb,confidence=?,last_verified_at=?,updated_at=now() WHERE id=?""",
            (*values, event_id),
        )
        changed = [key for key in reviewed if key in before and str(before[key]) != str(reviewed[key])]
    else:
        same_key = connection.execute(
            "SELECT * FROM calendar_events WHERE canonical_key=?", (reviewed["canonical_key"],)
        ).fetchone()
        if same_key:
            return save_event(connection, candidate, reviewed, same_key, model)
        event_id = connection.execute(
            """INSERT INTO calendar_events(canonical_key,title,summary,starts_at,ends_at,all_day,
                 date_precision,status,location,participants,audiences,tags,confidence,last_verified_at)
               VALUES (?,?,?,?,?,?,?,?,?,?::jsonb,?::jsonb,?::jsonb,?,?) RETURNING id""", values
        ).fetchone()["id"]
        changed = list(reviewed)
    revision = connection.execute(
        "SELECT COALESCE(max(revision_no),0)+1 AS revision FROM calendar_event_revisions WHERE event_id=?",
        (event_id,),
    ).fetchone()["revision"]
    snapshot = connection.execute("SELECT * FROM calendar_events WHERE id=?", (event_id,)).fetchone()
    connection.execute(
        """INSERT INTO calendar_event_revisions(event_id,revision_no,snapshot,changed_fields,reason,model)
           VALUES (?,?,?::jsonb,?::jsonb,?,?)""",
        (event_id, revision, json.dumps(dict(snapshot), ensure_ascii=False, default=str),
         json.dumps(changed, ensure_ascii=False), reviewed["reason"], model),
    )
    payload = candidate["payload"] if isinstance(candidate["payload"], dict) else json.loads(candidate["payload"])
    connection.execute(
        """INSERT INTO calendar_event_sources(event_id,source_document_id,source_url,evidence)
           VALUES (?,?,?,?::jsonb) ON CONFLICT(event_id,source_url) DO UPDATE SET
           source_document_id=excluded.source_document_id,evidence=excluded.evidence,observed_at=now()""",
        (event_id, candidate["source_document_id"], payload.get("source_url", ""),
         json.dumps({"candidate_id": candidate["id"], "text": candidate["candidate_text"]}, ensure_ascii=False)),
    )
    connection.commit()
    return event_id


async def process_event(connection, runtime_role="analyst"):
    candidate = workflow.claim(connection, "event_candidates")
    if not candidate:
        return False
    print(
        f"Аналітик узяв подію #{candidate['id']} "
        f"(спроба {int(candidate.get('attempts') or 0) + 1})",
        flush=True,
    )
    try:
        payload = candidate["payload"] if isinstance(candidate["payload"], dict) else json.loads(candidate["payload"])
        document = connection.execute(
            "SELECT * FROM source_documents WHERE id=?", (candidate["source_document_id"],)
        ).fetchone() or {}
        manual_enrichment = bool(payload.get("manual_enrichment"))
        if manual_enrichment:
            existing = connection.execute(
                "SELECT * FROM calendar_events WHERE id=?", (payload.get("event_id"),)
            ).fetchone()
            score = 1.0 if existing else 0.0
        else:
            score, existing = similar_event(connection, candidate["candidate_key"])
        model = codex_runtime.model(runtime_role)
        prompt = (
            event_enrichment_prompt(candidate, existing)
            if manual_enrichment else
            event_prompt(candidate, document, existing if score >= .72 else None)
        )
        reviewed = call_structured(
            prompt, EVENT_SCHEMA, model,
            "event_manual_enrichment" if manual_enrichment else "event_review",
            web=True,
            runtime_role=runtime_role,
        )
        if reviewed["action"] == "reject":
            workflow.finish(connection, "event_candidates", candidate["id"], "rejected", reviewed["reason"])
            return True
        if reviewed["action"] == "manual_review":
            workflow.finish(connection, "event_candidates", candidate["id"], "manual_review", reviewed["reason"])
            return True
        target = existing if manual_enrichment or score >= .82 or reviewed["action"] == "merge" else None
        event_id = save_event(connection, candidate, reviewed, target, model)
        if event_id is None:
            workflow.finish(
                connection, "event_candidates", candidate["id"], "rejected",
                "Подія вже в минулому — до календаря не додано",
            )
            return True
        workflow.finish(connection, "event_candidates", candidate["id"], "merged" if target else "confirmed")
    except Exception as error:
        status, attempt, maximum = queue_failure_status(
            candidate, error, "ANALYST_EVENT_MAX_ATTEMPTS"
        )
        workflow.finish(connection, "event_candidates", candidate["id"], status, str(error))
        print(
            f"Помилка події #{candidate['id']} (спроба {attempt}/{maximum}, "
            f"новий статус {status}): {type(error).__name__}: {error}",
            flush=True,
        )
        if status == "queued":
            await asyncio.sleep(max(2, int(os.getenv("ANALYST_RETRY_DELAY", "15"))))
    return True


async def run(once=False):
    load_dotenv(ROOT / ".env")
    write_schemas()
    connection = db.connect()
    workflow.recover_stale(connection)
    allowed = collector.read_allowed_subscriber_ids()
    proxy, _ = collector.configure_proxy()
    request = HTTPXRequest(proxy=proxy, httpx_kwargs={"trust_env": False})
    bot = Bot(token=collector.required_env("BOT_TOKEN"), request=request)
    application = SimpleNamespace(bot=bot)
    print(
        f"Редактор запущений. Модель: {codex_runtime.model('editor')}; "
        f"максимум спроб: {max(1, int(os.getenv('EDITOR_MAX_ATTEMPTS', '3')))}",
        flush=True,
    )
    async with bot:
        while True:
            worked = await process_note(connection, application, allowed)
            if once or not worked:
                if once:
                    break
                await asyncio.sleep(max(2, int(os.getenv("EDITOR_POLL_SECONDS", "5"))))
    connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Редакторська черга і календар подій")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.once))
