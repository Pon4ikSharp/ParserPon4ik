"""Local-only FastAPI control panel for ParserPon4ik."""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import urllib.parse
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import analyst, calendar_dates, db, grok_local_v3, influence_dashboard, process_manager, source_admin, translation as translation_service, workflow

ROOT = db.ROOT
load_dotenv(ROOT / ".env")
WEB_ROOT = ROOT / "webui"
KYIV = ZoneInfo("Europe/Kyiv")
ACTION_TOKEN = secrets.token_urlsafe(32)
AUTH_USER = os.getenv("WEB_AUTH_USER", "").strip()
AUTH_PASSWORD_HASH = os.getenv("WEB_AUTH_PASSWORD_HASH", "").strip()
SESSION_SECRET = os.getenv("WEB_SESSION_SECRET", "").strip()
SESSION_COOKIE_NAME = "ponchik_session"
SESSION_TTL_SECONDS = max(3600, int(os.getenv("WEB_SESSION_TTL_HOURS", "24")) * 3600)
SESSION_COOKIE_SECURE = os.getenv("WEB_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}
ALLOWED_HOSTS = [x.strip() for x in os.getenv("WEB_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver").split(",") if x.strip()]
PARSER_HEARTBEAT_PATH = ROOT / "runtime" / "parser_heartbeat.json"
LIGHT_STATS_TTL_SECONDS = 60
LIGHT_STATS_LOCK = threading.Lock()
LIGHT_STATS_CACHE = {"at": 0.0, "value": None}
DASHBOARD_CACHE_TTL_SECONDS = 30
DASHBOARD_CACHE_LOCK = threading.Lock()
DASHBOARD_CACHE = {"at": 0.0, "value": None}
PRIMARY_INFO_PERIOD_START = "2026-09-13 21:01:00+00"  # 14.09.2026 00:01 Europe/Kyiv


def local_display(value):
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(KYIV).strftime("%d.%m.%Y %H:%M")


def calendar_display(value, all_day=False):
    rendered = local_display(value)
    return rendered.split(" ", 1)[0] if all_day and rendered != "—" else rendered


def json_pretty(value):
    if value in (None, "", [], {}):
        return "—"
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


templates = Jinja2Templates(directory=WEB_ROOT / "templates")
templates.env.filters["localtime"] = local_display
templates.env.filters["calendartime"] = calendar_display
templates.env.filters["prettyjson"] = json_pretty


def page(request, name, **context):
    current_parser = context.get("nav_parser") or context.get("parser") or parser_health_snapshot()
    context.update(
        request=request,
        action_token=ACTION_TOKEN,
        current_path=request.url.path,
        now=datetime.now(KYIV),
        nav_parser=current_parser,
    )
    return templates.TemplateResponse(request=request, name=name, context=context)


def connect(readonly=True):
    return db.connect(readonly=readonly)


def utc_boundary(value, end=False):
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KYIV)
    return parsed.astimezone(timezone.utc)


def require_action_token(x_ponchik_token: str = Header(default="")):
    if not hmac.compare_digest(x_ponchik_token, ACTION_TOKEN):
        raise HTTPException(status_code=403, detail="Недійсний локальний ключ дії")


def _password_matches(password: str) -> bool:
    try:
        scheme, iterations_text, salt_hex, expected_hex = AUTH_PASSWORD_HASH.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations_text)
        )
        return hmac.compare_digest(digest.hex(), expected_hex)
    except (TypeError, ValueError):
        return False


def _session_value(username: str) -> str:
    expires = int(time.time()) + SESSION_TTL_SECONDS
    payload_raw = json.dumps({"u": username, "exp": expires}, separators=(",", ":")).encode("utf-8")
    payload = base64.urlsafe_b64encode(payload_raw).decode("ascii").rstrip("=")
    signature = hmac.new(SESSION_SECRET.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _valid_session(value: str | None) -> bool:
    if not value or not SESSION_SECRET:
        return False
    try:
        payload, signature = value.rsplit(".", 1)
        expected = hmac.new(SESSION_SECRET.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        padded = payload + "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        return data.get("u") == AUTH_USER and int(data.get("exp", 0)) >= int(time.time())
    except (ValueError, TypeError, json.JSONDecodeError):
        return False


def _safe_next(value: str | None) -> str:
    value = (value or "/").strip()
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


@asynccontextmanager
async def lifespan(_app):
    yield
    # A panel restart, port conflict, browser close, or server error must never
    # stop collection.  Workers are stopped only by the explicit maintenance
    # action unless an operator deliberately opts into the old coupling.
    if os.getenv("WEB_STOP_WORKERS_ON_EXIT", "false").lower() in {"1", "true", "yes"}:
        process_manager.stop_all()


app = FastAPI(title="Золотий пончик — панель керування", docs_url=None, redoc_url=None, lifespan=lifespan)
app.state.action_token = ACTION_TOKEN
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=ALLOWED_HOSTS,
)
app.mount("/static", StaticFiles(directory=WEB_ROOT / "static"), name="static")


@app.middleware("http")
async def require_web_login(request: Request, call_next):
    path = request.url.path
    if path == "/login" or path.startswith("/static/") or path == "/favicon.ico":
        return await call_next(request)
    if _valid_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"ok": False, "error": "authentication required"}, status_code=401)
    next_path = path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(url=f"/login?{urllib.parse.urlencode({'next': next_path})}", status_code=303)


@app.get("/login")
def login_form(request: Request, next: str = "/"):
    if _valid_session(request.cookies.get(SESSION_COOKIE_NAME)):
        return RedirectResponse(url=_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"request": request, "error": "", "next_path": _safe_next(next)},
    )


@app.post("/login")
async def login_submit(request: Request):
    raw = (await request.body()).decode("utf-8", errors="replace")
    form = urllib.parse.parse_qs(raw, keep_blank_values=True)
    username = (form.get("username") or [""])[0]
    password = (form.get("password") or [""])[0]
    next_path = _safe_next((form.get("next") or ["/"])[0])
    if not AUTH_USER or not AUTH_PASSWORD_HASH or not SESSION_SECRET:
        error = "Авторизацію на сервері не налаштовано."
        status_code = 503
    elif hmac.compare_digest(username, AUTH_USER) and _password_matches(password):
        response = RedirectResponse(url=next_path, status_code=303)
        response.set_cookie(
            SESSION_COOKIE_NAME,
            _session_value(username),
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            secure=SESSION_COOKIE_SECURE,
            samesite="lax",
            path="/",
        )
        return response
    else:
        error = "Невірний логін або пароль."
        status_code = 401
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"request": request, "error": error, "next_path": next_path},
        status_code=status_code,
    )


@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


@app.exception_handler(Exception)
async def unhandled_error(request: Request, error: Exception):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"ok": False, "error": str(error)}, status_code=500)
    response = page(request, "error.html", error=str(error))
    response.status_code = 500
    return response


def parser_health_snapshot():
    process = process_manager.status("collector")
    heartbeat = {}
    try:
        heartbeat = json.loads(PARSER_HEARTBEAT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        heartbeat = {}

    heartbeat_at = heartbeat.get("heartbeat_at") or ""
    age_seconds = None
    if heartbeat_at:
        try:
            parsed = datetime.fromisoformat(heartbeat_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age_seconds = max(
                0, int((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
            )
        except ValueError:
            age_seconds = None

    health_max_age = max(60, int(os.getenv("PARSER_HEALTH_MAX_AGE_SECONDS", "900")))
    healthy = bool(
        process["running"]
        and age_seconds is not None
        and age_seconds <= health_max_age
        and heartbeat.get("status") in {"starting", "running"}
    )
    return {
        **process,
        "healthy": healthy,
        "heartbeat_at": heartbeat_at,
        "heartbeat_age_seconds": age_seconds,
        "heartbeat_status": heartbeat.get("status") or "",
        "telegram_saved_session": int(heartbeat.get("telegram_saved") or 0),
        "rss_saved_session": int(heartbeat.get("rss_saved") or 0),
        "last_telegram_at": heartbeat.get("last_telegram_at") or "",
        "last_rss_cycle_at": heartbeat.get("last_rss_cycle_at") or "",
        "last_rss_saved_at": heartbeat.get("last_rss_saved_at") or "",
    }


def light_stats_snapshot(connection):
    now_mono = time.monotonic()
    cached = LIGHT_STATS_CACHE.get("value")
    if cached is not None and now_mono - LIGHT_STATS_CACHE["at"] < LIGHT_STATS_TTL_SECONDS:
        return cached

    with LIGHT_STATS_LOCK:
        now_mono = time.monotonic()
        cached = LIGHT_STATS_CACHE.get("value")
        if cached is not None and now_mono - LIGHT_STATS_CACHE["at"] < LIGHT_STATS_TTL_SECONDS:
            return cached

        parsed = connection.execute(
            """SELECT count(*) AS n
                 FROM source_documents
                WHERE NULLIF(created_at,'')::timestamptz >= ?::timestamptz""",
            (PRIMARY_INFO_PERIOD_START,),
        ).fetchone()
        primary = connection.execute(
            """SELECT
                 count(*) FILTER (WHERE q.status IN ('completed','clustered')) AS processed,
                 count(*) FILTER (WHERE q.decision='RELEVANT') AS accepted,
                 count(*) FILTER (WHERE q.decision='WATCH') AS watch,
                 count(*) FILTER (WHERE q.decision='CONTEXT_TRIGGER') AS context_trigger,
                 count(*) FILTER (WHERE q.decision='NEEDS_FULL_TEXT') AS needs_full_text,
                 count(*) FILTER (WHERE q.decision='NOT_RELEVANT') AS rejected,
                 count(*) FILTER (WHERE q.decision='LOCAL_SKIP') AS local_skip,
                 count(*) FILTER (WHERE q.status='error') AS errors
               FROM primary_info_queue_v1 q
               JOIN source_documents d ON d.id=q.source_document_id
              WHERE NULLIF(d.created_at,'')::timestamptz >= ?::timestamptz""",
            (PRIMARY_INFO_PERIOD_START,),
        ).fetchone()
        processed = int(primary["processed"] or 0)
        value = {
            "parsed_24h": int(parsed["n"] or 0),
            "analyzed_24h": processed,
            "accepted_24h": int(primary["accepted"] or 0),
            "watch_24h": int(primary["watch"] or 0),
            "context_trigger_24h": int(primary["context_trigger"] or 0),
            "needs_full_text_24h": int(primary["needs_full_text"] or 0),
            "rejected_24h": int(primary["rejected"] or 0),
            "local_skip_24h": int(primary["local_skip"] or 0),
            "errors_24h": int(primary["errors"] or 0),
        }
        value["not_analyzed_24h"] = max(0, value["parsed_24h"] - processed)
        LIGHT_STATS_CACHE["value"] = value
        LIGHT_STATS_CACHE["at"] = now_mono
        return value


def modern_dashboard_snapshot(connection):
    route_rows = connection.execute(
        """SELECT p.route,count(*) AS total
             FROM info_prefilter_results_v1 p
             JOIN source_documents d ON d.id=p.source_document_id
            WHERE COALESCE(NULLIF(d.published_at,'')::timestamptz,
                           NULLIF(d.created_at,'')::timestamptz) >= now()-interval '24 hours'
              AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                           NULLIF(d.created_at,'')::timestamptz) <= now()+interval '5 minutes'
            GROUP BY p.route"""
    ).fetchall()
    routes = {str(row["route"]): int(row["total"] or 0) for row in route_rows}
    for key in ("MUST_ANALYZE","ANALYZE","HOLD","DROP_SOFT","DROP_HARD","DUPLICATE"):
        routes.setdefault(key, 0)
    route_total = max(1, sum(routes.values()))
    route_items = [
        {"key": key, "value": routes[key], "pct": round(routes[key] * 100 / route_total, 1)}
        for key in ("MUST_ANALYZE","ANALYZE","HOLD","DROP_SOFT","DROP_HARD","DUPLICATE")
    ]

    recent = [dict(row) for row in connection.execute(
        """SELECT d.id,d.source_title,d.source_kind,d.source_url,d.published_at,d.created_at,
                  left(regexp_replace(COALESCE(NULLIF(d.full_text,''),NULLIF(d.feed_text,''),d.source_text,''),
                                      E'[\\n\\r\\t]+',' ','g'),170) AS preview,
                  p.route,p.processed_at
             FROM source_documents d
             LEFT JOIN info_prefilter_results_v1 p ON p.source_document_id=d.id
            WHERE COALESCE(NULLIF(d.published_at,'')::timestamptz,
                           NULLIF(d.created_at,'')::timestamptz) <= now()+interval '5 minutes'
            ORDER BY COALESCE(NULLIF(d.published_at,'')::timestamptz,
                              NULLIF(d.created_at,'')::timestamptz) DESC NULLS LAST,d.id DESC
            LIMIT 8"""
    ).fetchall()]

    cluster = connection.execute(
        """SELECT c.id,c.canonical_document_id,c.unique_sources,c.spread_count,c.unique_domains,
                  c.unique_telegram_channels,c.cross_platform,c.first_seen_at,c.last_seen_at,
                  d.source_title,
                  left(regexp_replace(COALESCE(NULLIF(d.full_text,''),NULLIF(d.feed_text,''),d.source_text,''),
                                      E'[\\n\\r\\t]+',' ','g'),190) AS preview
             FROM primary_info_clusters_v1 c
             JOIN source_documents d ON d.id=c.canonical_document_id
            WHERE c.unique_sources>=2
            ORDER BY c.last_seen_at DESC NULLS LAST,c.unique_sources DESC,c.spread_count DESC
            LIMIT 1"""
    ).fetchone()
    spread = None
    if cluster:
        spread = dict(cluster)
        spread["members"] = [dict(row) for row in connection.execute(
            """SELECT d.id,d.source_title,d.source_kind,d.source_url,d.published_at,m.match_type
                 FROM primary_info_cluster_members_v1 m
                 JOIN source_documents d ON d.id=m.source_document_id
                WHERE m.cluster_id=?
                ORDER BY COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                  NULLIF(d.created_at,'')::timestamptz) ASC NULLS LAST,d.id
                LIMIT 8""",
            (cluster["id"],),
        ).fetchall()]

    telegram_terms = [dict(row) for row in connection.execute(
        """SELECT m.normalized_marker AS term, min(m.channel_title) AS channel_title,
                      count(*) FILTER (WHERE COALESCE(NULLIF(d.published_at,'')::timestamptz,NULLIF(d.created_at,'')::timestamptz)>=now()-interval '24 hours') AS count_24h,
                      count(*) AS count_7d,
                      (count(*) FILTER (WHERE p.route IN ('MUST_ANALYZE','ANALYZE'))*4 + LEAST(count(*),20))::int AS candidate_score,
                      EXISTS(SELECT 1 FROM telegram_lexicon_terms_v1 l WHERE l.term_type='hashtag' AND lower(l.term)=m.normalized_marker AND l.promoted) AS promoted
                 FROM telegram_document_markers_v1 m
                 JOIN source_documents d ON d.id=m.source_document_id
            LEFT JOIN info_prefilter_results_v1 p ON p.source_document_id=d.id
                WHERE m.marker_type='hashtag' AND m.normalized_marker LIKE '#%'
                  AND COALESCE(NULLIF(d.published_at,'')::timestamptz,NULLIF(d.created_at,'')::timestamptz)>=now()-interval '7 days'
                  AND COALESCE(NULLIF(d.published_at,'')::timestamptz,NULLIF(d.created_at,'')::timestamptz)<=now()+interval '5 minutes'
                GROUP BY m.normalized_marker
                ORDER BY promoted DESC,candidate_score DESC,count_24h DESC,count_7d DESC,term
                LIMIT 12"""
    ).fetchall()]

    latest_prefilter = connection.execute(
        """SELECT rules_version,processed_at
             FROM info_prefilter_results_v1
            ORDER BY processed_at DESC NULLS LAST LIMIT 1"""
    ).fetchone()
    latest_lexicon = connection.execute(
        """SELECT snapshot_at,count(*) AS total
             FROM telegram_lexicon_terms_v1
            GROUP BY snapshot_at ORDER BY snapshot_at DESC NULLS LAST LIMIT 1"""
    ).fetchone()
    newest_doc = connection.execute(
        """SELECT source_title,created_at
             FROM source_documents
            ORDER BY NULLIF(created_at,'')::timestamptz DESC NULLS LAST,id DESC LIMIT 1"""
    ).fetchone()

    activity = []
    parser = parser_health_snapshot()
    activity.append({
        "title": "collector online" if parser["healthy"] else "collector needs attention",
        "detail": f"PID {parser.get('pid') or '—'} · heartbeat {parser.get('heartbeat_age_seconds') if parser.get('heartbeat_age_seconds') is not None else '—'} с",
        "time": parser.get("heartbeat_at") or "",
        "tone": "green" if parser["healthy"] else "amber",
    })
    if latest_prefilter:
        activity.append({
            "title": latest_prefilter["rules_version"] or "prefilter",
            "detail": "Останнє рішення первинного фільтра",
            "time": latest_prefilter["processed_at"],
            "tone": "blue",
        })
    if latest_lexicon:
        activity.append({
            "title": "lexicon refresh",
            "detail": f"Термінів у snapshot: {int(latest_lexicon['total'] or 0):,}".replace(",", " "),
            "time": latest_lexicon["snapshot_at"],
            "tone": "amber",
        })
    if newest_doc:
        activity.append({
            "title": "new source document",
            "detail": newest_doc["source_title"] or "Нове джерело",
            "time": newest_doc["created_at"],
            "tone": "slate",
        })

    return {
        "routes": routes,
        "route_items": route_items,
        "route_total": route_total,
        "recent": recent,
        "spread": spread,
        "telegram_terms": telegram_terms,
        "watchlist": ["Лавров","Захарова","Пєсков","Медведєв","Небензя","Путін","Матвієнко"],
        "activity": activity,
    }


ROUTE_LABELS_UK = {
    "MUST_ANALYZE": "Обов'язково аналізувати",
    "ANALYZE": "На аналіз",
    "HOLD": "Очікує сигналів",
    "DROP_SOFT": "Відсіяно",
    "DROP_HARD": "Технічний відсів",
    "DUPLICATE": "Дублікат",
}


def dashboard_v3_snapshot(connection):
    now_mono = time.monotonic()
    cached = DASHBOARD_CACHE.get("value")
    if cached is not None and now_mono - float(DASHBOARD_CACHE.get("at", 0.0)) < DASHBOARD_CACHE_TTL_SECONDS:
        return cached

    with DASHBOARD_CACHE_LOCK:
        now_mono = time.monotonic()
        cached = DASHBOARD_CACHE.get("value")
        if cached is not None and now_mono - float(DASHBOARD_CACHE.get("at", 0.0)) < DASHBOARD_CACHE_TTL_SECONDS:
            return cached

        candidate_cutoff_text = (
            datetime.now(timezone.utc) - timedelta(hours=48)
        ).isoformat(timespec="seconds")
        ai_pending = grok_local_v3.backlog_counts(connection)
        ai_row = connection.execute(
            """SELECT count(*)::int AS processed_total,
                      count(*) FILTER (WHERE g.decision='KEEP')::int AS keep_count,
                      count(*) FILTER (WHERE g.decision='CONTEXT')::int AS context_count,
                      count(*) FILTER (WHERE g.decision='REVIEW')::int AS review_count,
                      count(*) FILTER (WHERE g.decision='DROP')::int AS drop_count,
                      max(g.updated_at) AS last_processed_at
                 FROM grok_document_analysis_v1 g
                 JOIN source_documents d ON d.id=g.source_document_id
                WHERE COALESCE(NULLIF(d.published_at,'')::timestamptz,
                               NULLIF(d.created_at,'')::timestamptz)
                      >= (?::timestamp AT TIME ZONE 'Europe/Kyiv')
                  AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                               NULLIF(d.created_at,'')::timestamptz)
                      <= now()+interval '5 minutes'""",
            (grok_local_v3.SCOPE_START,),
        ).fetchone()
        ai_processed = int(ai_row["processed_total"] or 0)
        ai_must = int(ai_pending.get("MUST_ANALYZE", 0))
        ai_analyze = int(ai_pending.get("ANALYZE", 0))
        ai_waiting = ai_must + ai_analyze
        ai_total = ai_processed + ai_waiting
        ai_snapshot = {
            "processed": ai_processed, "pending": ai_waiting,
            "pending_must": ai_must, "pending_analyze": ai_analyze,
            "keep": int(ai_row["keep_count"] or 0),
            "context": int(ai_row["context_count"] or 0),
            "review": int(ai_row["review_count"] or 0),
            "drop": int(ai_row["drop_count"] or 0),
            "last_processed_at": ai_row["last_processed_at"],
            "completion_percent": round(ai_processed * 100 / ai_total, 1) if ai_total else 100.0,
            "scope_label": "від 28.09.2026",
        }

        route_rows = connection.execute(
            """SELECT p.route,count(*) AS total
                 FROM info_prefilter_results_v1 p
                 JOIN source_documents d ON d.id=p.source_document_id
                WHERE d.created_at>=?
                  AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                               NULLIF(d.created_at,'')::timestamptz)>=now()-interval '24 hours'
                  AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                               NULLIF(d.created_at,'')::timestamptz)<=now()+interval '5 minutes'
                GROUP BY p.route""",
            (candidate_cutoff_text,),
        ).fetchall()
        routes = {str(row["route"]): int(row["total"] or 0) for row in route_rows}
        for key in ("MUST_ANALYZE","ANALYZE","HOLD","DROP_SOFT","DROP_HARD","DUPLICATE"):
            routes.setdefault(key, 0)

        important = [dict(row) for row in connection.execute(
            """SELECT d.id,d.source_title,d.source_kind,d.source_url,d.published_at,d.created_at,
                      left(regexp_replace(COALESCE(NULLIF(d.feed_text,''),NULLIF(d.source_text,''),
                                                   NULLIF(d.full_text,''),''),
                                          E'[\\n\\r\\t]+',' ','g'),220) AS preview,
                      p.route,p.processed_at,p.domain_score,p.influence_score,p.spread_score
                 FROM info_prefilter_results_v1 p
                 JOIN source_documents d ON d.id=p.source_document_id
                WHERE p.route IN ('MUST_ANALYZE','ANALYZE')
                  AND d.created_at>=?
                  AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                               NULLIF(d.created_at,'')::timestamptz)>=now()-interval '24 hours'
                  AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                               NULLIF(d.created_at,'')::timestamptz)<=now()+interval '5 minutes'
                ORDER BY COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                  NULLIF(d.created_at,'')::timestamptz) DESC,d.id DESC
                LIMIT 10""",
            (candidate_cutoff_text,),
        ).fetchall()]

        active_stories = [dict(row) for row in connection.execute(
            """SELECT c.id,c.canonical_document_id,c.analysis_document_id,c.signal_document_id,
                      c.unique_sources,c.spread_count,c.unique_domains,
                      c.unique_telegram_channels,c.cross_platform,c.first_seen_at,c.last_seen_at,
                      d.source_title,
                      left(regexp_replace(COALESCE(NULLIF(d.feed_text,''),NULLIF(d.source_text,''),
                                                   NULLIF(d.full_text,''),''),
                                          E'[\n\r\t]+',' ','g'),240) AS preview,
                      p.route,p.domain_score,p.influence_score,p.spread_score
                 FROM primary_info_clusters_v1 c
                 JOIN source_documents d ON d.id=c.canonical_document_id
                 JOIN info_prefilter_results_v1 p ON p.source_document_id=c.canonical_document_id
                WHERE c.unique_sources>=2
                  AND p.route IN ('MUST_ANALYZE','ANALYZE')
                  AND c.last_seen_at>=now()-interval '6 hours'
                ORDER BY c.unique_sources DESC,c.spread_count DESC,c.last_seen_at DESC
                LIMIT 5"""
        ).fetchall()]

        story_graph = None
        if active_stories:
            story_graph = dict(active_stories[0])
            graph_rows = [dict(row) for row in connection.execute(
                """WITH ranked AS (
                       SELECT d.id,d.source_id,d.source_title,d.source_kind,
                              d.published_at,d.created_at,
                              m.match_type,m.similarity,m.propagation_role,
                              CASE WHEN d.id=? THEN 0 ELSE 1 END AS canonical_rank,
                              COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                       NULLIF(d.created_at,'')::timestamptz) AS event_at,
                              row_number() OVER (
                                  PARTITION BY COALESCE(
                                      d.source_id::text,
                                      COALESCE(d.source_kind,'')||'::'||COALESCE(d.source_title,'')
                                  )
                                  ORDER BY CASE WHEN d.id=? THEN 0 ELSE 1 END,
                                           COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                                    NULLIF(d.created_at,'')::timestamptz),
                                           d.id
                              ) AS source_rank
                         FROM primary_info_cluster_members_v1 m
                         JOIN source_documents d ON d.id=m.source_document_id
                        WHERE m.cluster_id=?
                   )
                   SELECT id,source_id,source_title,source_kind,published_at,created_at,
                          match_type,similarity,propagation_role,canonical_rank,event_at
                     FROM ranked
                    WHERE source_rank=1
                    ORDER BY canonical_rank,event_at,id
                    LIMIT 7""",
                (
                    int(story_graph["canonical_document_id"]),
                    int(story_graph["canonical_document_id"]),
                    int(story_graph["id"]),
                ),
            ).fetchall()]

            origin = next(
                (row for row in graph_rows if int(row["id"]) == int(story_graph["canonical_document_id"])),
                graph_rows[0] if graph_rows else None,
            )
            followers = [
                row for row in graph_rows
                if origin is None or int(row["id"]) != int(origin["id"])
            ][:6]
            positions = [(68,18),(84,34),(88,58),(76,80),(55,84),(58,38)]
            match_labels = {
                "exact": "точний повтор",
                "near": "схожий текст",
                "canonical": "оригінал",
                "local_junk": "технічний збіг",
            }
            kind_labels = {
                "telegram": "Telegram",
                "rss": "сайт",
                "web": "сайт",
            }
            for index, member in enumerate(followers):
                x, y = positions[index]
                member["x"] = x
                member["y"] = y
                member["match_label"] = match_labels.get(
                    str(member.get("match_type") or "").lower(),
                    "повтор",
                )
                member["kind_label"] = kind_labels.get(
                    str(member.get("source_kind") or "").lower(),
                    str(member.get("source_kind") or "джерело"),
                )
                member["is_signal"] = (
                    story_graph.get("signal_document_id") is not None
                    and int(member["id"]) == int(story_graph["signal_document_id"])
                    and int(member["id"]) != int(story_graph["canonical_document_id"])
                )
            if origin is not None:
                origin["kind_label"] = kind_labels.get(
                    str(origin.get("source_kind") or "").lower(),
                    str(origin.get("source_kind") or "джерело"),
                )
            story_graph["origin"] = origin
            story_graph["members"] = followers
            story_graph["hidden_members"] = max(
                0,
                int(story_graph.get("unique_sources") or 1) - 1 - len(followers),
            )
            event_times = [
                row.get("event_at") for row in graph_rows if row.get("event_at") is not None
            ]
            if len(event_times) >= 2:
                duration_seconds = max(
                    0,
                    int((max(event_times) - min(event_times)).total_seconds()),
                )
                if duration_seconds < 60:
                    story_graph["duration_label"] = "менше 1 хв"
                elif duration_seconds < 3600:
                    story_graph["duration_label"] = f"{(duration_seconds + 59) // 60} хв"
                else:
                    hours, remainder = divmod(duration_seconds, 3600)
                    minutes = remainder // 60
                    story_graph["duration_label"] = (
                        f"{hours} год {minutes} хв" if minutes else f"{hours} год"
                    )
            else:
                story_graph["duration_label"] = "—"

        telegram_terms = [dict(row) for row in connection.execute(
            """SELECT m.normalized_marker AS term, min(m.channel_title) AS channel_title,
                      count(*) FILTER (WHERE COALESCE(NULLIF(d.published_at,'')::timestamptz,NULLIF(d.created_at,'')::timestamptz)>=now()-interval '24 hours') AS count_24h,
                      count(*) AS count_7d,
                      (count(*) FILTER (WHERE p.route IN ('MUST_ANALYZE','ANALYZE'))*4 + LEAST(count(*),20))::int AS candidate_score,
                      EXISTS(SELECT 1 FROM telegram_lexicon_terms_v1 l WHERE l.term_type='hashtag' AND lower(l.term)=m.normalized_marker AND l.promoted) AS promoted
                 FROM telegram_document_markers_v1 m
                 JOIN source_documents d ON d.id=m.source_document_id
            LEFT JOIN info_prefilter_results_v1 p ON p.source_document_id=d.id
                WHERE m.marker_type='hashtag' AND m.normalized_marker LIKE '#%'
                  AND COALESCE(NULLIF(d.published_at,'')::timestamptz,NULLIF(d.created_at,'')::timestamptz)>=now()-interval '7 days'
                  AND COALESCE(NULLIF(d.published_at,'')::timestamptz,NULLIF(d.created_at,'')::timestamptz)<=now()+interval '5 minutes'
                GROUP BY m.normalized_marker
                ORDER BY promoted DESC,candidate_score DESC,count_24h DESC,count_7d DESC,term
                LIMIT 12"""
        ).fetchall()]

        top_sources = [dict(row) for row in connection.execute(
            """SELECT COALESCE(NULLIF(d.source_title,''),'Без назви') AS source_title,
                      count(*) FILTER (WHERE p.route='MUST_ANALYZE') AS must_count,
                      count(*) FILTER (WHERE p.route='ANALYZE') AS analyze_count,
                      count(*) AS total
                 FROM info_prefilter_results_v1 p
                 JOIN source_documents d ON d.id=p.source_document_id
                WHERE p.route IN ('MUST_ANALYZE','ANALYZE')
                  AND lower(COALESCE(d.source_title,'')) NOT LIKE 'pravda%'
                  AND COALESCE(d.source_url,'') NOT ILIKE '%news-pravda.com/%'
                  AND d.created_at>=?
                  AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                               NULLIF(d.created_at,'')::timestamptz)>=now()-interval '24 hours'
                  AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                               NULLIF(d.created_at,'')::timestamptz)<=now()+interval '5 minutes'
                GROUP BY COALESCE(NULLIF(d.source_title,''),'Без назви')
                ORDER BY total DESC,source_title
                LIMIT 8""",
            (candidate_cutoff_text,),
        ).fetchall()]
        max_source_total = max([1, *[int(row["total"] or 0) for row in top_sources]])
        for row in top_sources:
            row["bar_percent"] = round(int(row["total"] or 0) * 100 / max_source_total, 1)

        relay_rows = [dict(row) for row in connection.execute(
            """SELECT COALESCE(NULLIF(d.source_title,''),'Pravda Network') AS source_title,
                      count(*) AS total,
                      count(*) FILTER (WHERE p.route='MUST_ANALYZE') AS must_count,
                      count(*) FILTER (WHERE p.route='ANALYZE') AS analyze_count,
                      count(*) FILTER (WHERE p.route='HOLD') AS hold_count,
                      count(*) FILTER (WHERE p.route='DROP_SOFT') AS drop_soft_count
                 FROM source_documents d
            LEFT JOIN info_prefilter_results_v1 p ON p.source_document_id=d.id
                WHERE (
                        lower(COALESCE(d.source_title,'')) LIKE 'pravda%'
                        OR COALESCE(d.source_url,'') ILIKE '%news-pravda.com/%'
                      )
                  AND d.created_at>=?
                  AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                               NULLIF(d.created_at,'')::timestamptz)>=now()-interval '24 hours'
                  AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                               NULLIF(d.created_at,'')::timestamptz)<=now()+interval '5 minutes'
                GROUP BY COALESCE(NULLIF(d.source_title,''),'Pravda Network')
                ORDER BY total DESC,source_title""",
            (candidate_cutoff_text,),
        ).fetchall()]
        relay_network = {
            "name": "Pravda Network",
            "total": sum(int(row["total"] or 0) for row in relay_rows),
            "must_count": sum(int(row["must_count"] or 0) for row in relay_rows),
            "analyze_count": sum(int(row["analyze_count"] or 0) for row in relay_rows),
            "hold_count": sum(int(row["hold_count"] or 0) for row in relay_rows),
            "drop_soft_count": sum(int(row["drop_soft_count"] or 0) for row in relay_rows),
            "nodes": relay_rows,
        }

        attention_row = connection.execute(
            """SELECT
                 count(*) FILTER (
                     WHERE p.route='MUST_ANALYZE'
                       AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                    NULLIF(d.created_at,'')::timestamptz)>=now()-interval '30 minutes'
                 ) AS must_30m,
                 count(*) FILTER (
                     WHERE p.route='ANALYZE'
                       AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                    NULLIF(d.created_at,'')::timestamptz)>=now()-interval '30 minutes'
                 ) AS analyze_30m,
                 count(*) FILTER (
                     WHERE p.cluster_id IS NULL
                       AND jsonb_exists(p.reason_codes,'fast_prefilter_no_spread')
                 ) AS heavy_backlog
                 FROM info_prefilter_results_v1 p
                 JOIN source_documents d ON d.id=p.source_document_id"""
        ).fetchone()
        hot_spread = int(connection.execute(
            """SELECT count(*) AS total
                 FROM primary_info_clusters_v1
                WHERE unique_sources>=5
                  AND last_seen_at>=now()-interval '2 hours'"""
        ).fetchone()["total"] or 0)

        attention = []
        must_30m = int(attention_row["must_30m"] or 0)
        analyze_30m = int(attention_row["analyze_30m"] or 0)
        heavy_backlog = int(attention_row["heavy_backlog"] or 0)
        if must_30m:
            attention.append({
                "tone": "critical",
                "title": f"{must_30m} нових важливих матеріалів",
                "detail": "За останні 30 хвилин",
                "href": "/originals?route=MUST_ANALYZE",
            })
        if hot_spread:
            attention.append({
                "tone": "signal",
                "title": f"{hot_spread} сюжетів активно поширюються",
                "detail": "П'ять або більше джерел за останні 2 години",
                "href": "/influence",
            })
        if analyze_30m:
            attention.append({
                "tone": "info",
                "title": f"{analyze_30m} матеріалів на аналіз",
                "detail": "Нові кандидати за останні 30 хвилин",
                "href": "/originals?route=ANALYZE",
            })
        attention.append({
            "tone": "ok" if heavy_backlog < 100 else "warning",
            "title": f"{heavy_backlog} матеріалів у фоновому поширенні",
            "detail": "Не блокує попередню обробку",
            "href": "/influence",
        })

        value = {
            "routes": routes,
            "ai": ai_snapshot,
            "important": important,
            "active_stories": active_stories,
            "story_graph": story_graph,
            "telegram_terms": telegram_terms,
            "top_sources": top_sources,
            "relay_network": relay_network,
            "attention": attention[:5],
            "heavy_backlog": heavy_backlog,
            "route_labels": ROUTE_LABELS_UK,
        }
        DASHBOARD_CACHE["at"] = time.monotonic()
        DASHBOARD_CACHE["value"] = value
        return value


@app.get("/")
def dashboard(request: Request):
    connection = connect()
    try:
        parser = parser_health_snapshot()
        modern = dashboard_v3_snapshot(connection)
        attention = list(modern["attention"])
        if not parser["healthy"]:
            attention.insert(0, {
                "tone": "critical",
                "title": "Збір потребує уваги",
                "detail": "Перевірте стан процесу та останній heartbeat",
                "href": "/logs?role=collector",
            })
        modern_for_page = dict(modern)
        modern_for_page["attention"] = attention[:5]
        return page(
            request,
            "dashboard.html",
            parser=parser,
            nav_parser=parser,
            modern=modern_for_page,
        )
    finally:
        connection.close()


def collector_queue_snapshot(connection):
    """Use one bounded collector queue definition throughout the panel."""
    maximum = max(1, int(os.getenv("COLLECTOR_MAX_ATTEMPTS", "3")))
    if db.has_table(connection, "classification_queue"):
        row = connection.execute(
            """SELECT
              count(*) FILTER (WHERE status IN ('pending','processing','paused_limit')) AS collector_pending,
              count(*) FILTER (WHERE status='pending') AS classification_waiting,
              count(*) FILTER (WHERE status='processing') AS classification_processing,
              count(*) FILTER (WHERE status='completed') AS classification_completed,
              count(*) FILTER (WHERE status='error') AS classification_error,
              count(*) FILTER (WHERE status='paused_limit') AS classification_paused_limit,
              count(*) FILTER (WHERE br_status IN ('pending','screening','ready','processing','paused_limit')) AS br_unprocessed,
              count(*) FILTER (WHERE info_status IN ('pending','screening','ready','processing','paused_limit')) AS info_unprocessed,
              count(*) FILTER (WHERE status='paused_limit') AS collector_retries,
              count(*) FILTER (WHERE status='error') AS collector_attention
              FROM classification_queue"""
        ).fetchone()
        pending = int(row["collector_pending"] or 0)
        return {
            "collector_unprocessed": pending,
            "br_unprocessed": int(row["br_unprocessed"] or 0),
            "info_unprocessed": int(row["info_unprocessed"] or 0),
            "collector_pending": pending,
            "collector_retries": int(row["collector_retries"] or 0),
            "collector_attention": int(row["collector_attention"] or 0),
            "collector_max_attempts": maximum,
            "classification_waiting": int(row["classification_waiting"] or 0),
            "classification_processing": int(row["classification_processing"] or 0),
            "classification_completed": int(row["classification_completed"] or 0),
            "classification_error": int(row["classification_error"] or 0),
            "classification_paused_limit": int(row["classification_paused_limit"] or 0),
        }
    max_age_hours = max(1, int(os.getenv("RECOVERY_MAX_AGE_HOURS", "48")))
    row = connection.execute(
        f"""WITH base AS (
          SELECT d.id,s.for_br,s.for_info,m.status AS br_status,
                 i.status AS info_status,d.br_attempts,d.info_attempts
            FROM source_documents d
            JOIN sources s ON s.id=d.source_id
            LEFT JOIN messages m
              ON m.chat_id=d.source_chat_id AND m.message_id=d.source_message_id
            LEFT JOIN info_influence i ON i.source_document_id=d.id
           WHERE s.enabled
             AND length(trim(COALESCE(NULLIF(d.full_text,''),d.source_text,'')))>0
             AND (NULLIF(d.published_at,'') IS NULL
                  OR NULLIF(d.published_at,'')::timestamptz<=now()+interval '5 minutes')
             AND COALESCE(NULLIF(d.published_at,'')::timestamptz,
                          NULLIF(d.created_at,'')::timestamptz)
                 >=now()-({max_age_hours}||' hours')::interval
             AND NOT (d.source_kind='rss' AND EXISTS(
               SELECT 1 FROM rss_entries r
                WHERE r.feed_url=d.feed_url AND r.entry_url=d.source_url
                  AND r.status='bootstrap'))
        ) SELECT
          count(*) FILTER (WHERE
            (for_br AND br_status IS NULL) OR
            (for_info AND info_status IS NULL)) AS collector_unprocessed,
          count(*) FILTER (WHERE for_br AND br_status IS NULL) AS br_unprocessed,
          count(*) FILTER (WHERE for_info AND info_status IS NULL) AS info_unprocessed,
          count(*) FILTER (WHERE
            (for_br AND (br_status IS NULL OR
              (br_status='error' AND br_attempts<{maximum}))) OR
            (for_info AND (info_status IS NULL OR
              (info_status='error' AND info_attempts<{maximum})))) AS collector_pending,
          count(*) FILTER (WHERE
            (for_br AND br_status='error' AND br_attempts<{maximum}) OR
            (for_info AND info_status='error' AND info_attempts<{maximum})) AS collector_retries,
          count(*) FILTER (WHERE
            (for_br AND br_status='error' AND br_attempts>={maximum}) OR
            (for_info AND info_status='error' AND info_attempts>={maximum})) AS collector_attention
        FROM base"""
    ).fetchone()
    return {
        "collector_unprocessed": int(row["collector_unprocessed"] or 0),
        "br_unprocessed": int(row["br_unprocessed"] or 0),
        "info_unprocessed": int(row["info_unprocessed"] or 0),
        "collector_pending": int(row["collector_pending"] or 0),
        "collector_retries": int(row["collector_retries"] or 0),
        "collector_attention": int(row["collector_attention"] or 0),
        "collector_max_attempts": maximum,
    }


def dashboard_snapshot(connection):
    counts = dict(connection.execute(
        """SELECT
          (SELECT count(*) FROM source_documents) AS originals,
          (SELECT count(*) FROM source_documents
             WHERE NULLIF(published_at,'')::timestamptz>=now()-interval '24 hours') AS originals_24h,
          (SELECT count(*) FROM source_documents
             WHERE NULLIF(published_at,'')::timestamptz>now()+interval '5 minutes') AS future_dated,
          (SELECT count(*) FROM info_influence WHERE status='accepted') AS influence,
          (SELECT count(*) FROM calendar_events WHERE status IN ('tentative','confirmed')
             AND (starts_at>=now() OR starts_at IS NULL)) AS upcoming"""
    ).fetchone())
    recent = [dict(row) for row in connection.execute(
        """SELECT id,published_at,source_title,left(source_text,260) AS preview,
                  source_url,content_status
           FROM source_documents ORDER BY published_at DESC NULLS LAST,id DESC LIMIT 8"""
    ).fetchall()]
    days = [dict(row) for row in connection.execute(
        """SELECT date_trunc('day',NULLIF(published_at,'')::timestamptz AT TIME ZONE 'Europe/Kyiv') AS day,
                  count(*) AS total
           FROM source_documents WHERE NULLIF(published_at,'')::timestamptz>=now()-interval '7 days'
             AND NULLIF(published_at,'')::timestamptz<=now()
           GROUP BY 1 ORDER BY 1"""
    ).fetchall()]
    return {
        "counts": counts, "recent": recent, "days": days,
        "queues": [], "funnel": [],
        "processes": process_manager.all_statuses(),
        # The overview must never run the expensive collector backlog join.
        "workloads": workload_snapshot(connection, {"collector_pending": 0}),
    }


def workload_snapshot(connection, collector_queue=None):
    calls = {
        row["role"]: dict(row) for row in connection.execute(
            """SELECT role,count(*) AS calls_24h,
                      COALESCE(sum(input_chars),0) AS input_24h,
                      COALESCE(sum(output_chars),0) AS output_24h
               FROM model_usage WHERE created_at>=now()-interval '24 hours'
               GROUP BY role"""
        ).fetchall()
    }
    collector_queue = collector_queue or collector_queue_snapshot(connection)
    note_pending = connection.execute(
        "SELECT count(*) FROM note_drafts WHERE status IN ('queued','processing','manual_review','failed')"
    ).fetchone()[0]
    event_pending = connection.execute(
        "SELECT count(*) FROM event_candidates WHERE status IN ('queued','processing','manual_review','failed')"
    ).fetchone()[0]
    active_note = connection.execute(
        "SELECT id FROM note_drafts WHERE status='processing' ORDER BY locked_at LIMIT 1"
    ).fetchone()
    active_event = connection.execute(
        "SELECT id FROM event_candidates WHERE status='processing' ORDER BY locked_at LIMIT 1"
    ).fetchone()
    active_job = connection.execute(
        "SELECT id,job_type FROM analysis_jobs WHERE status='running' ORDER BY started_at LIMIT 1"
    ).fetchone()
    queued_jobs = connection.execute(
        "SELECT count(*) FROM analysis_jobs WHERE status='queued'"
    ).fetchone()[0]
    translation_pending = connection.execute(
        "SELECT count(*) FROM document_translations WHERE status IN ('queued','processing')"
    ).fetchone()[0]
    statuses = process_manager.all_statuses()
    parser_only = statuses["collector"].get("mode") == "parser_only"
    result = {
        "collector": {
            "activity": (
                "Лише збирання: Telegram і RSS, без Codex"
                if parser_only else
                "Первинний відбір архіву"
                if collector_queue["collector_pending"] else
                "Моніторинг Telegram і RSS"
            ),
            "backlog": collector_queue["collector_pending"],
        },
        "editor": {
            "activity": f"Замітка #{active_note['id']}" if active_note else "Очікує нові чернетки",
            "backlog": note_pending,
        },
        "analyst": {
            "activity": (f"Завдання #{active_job['id']} · {active_job['job_type']}"
                         if active_job else
                         f"Переклад оригіналу" if translation_pending else
                         f"Подія #{active_event['id']}" if active_event else "Очікує ваш запит"),
            "backlog": queued_jobs + translation_pending + event_pending,
        },
    }
    for role, item in result.items():
        item.update(calls.get(role, {"calls_24h": 0, "input_24h": 0, "output_24h": 0}))
        item["running"] = statuses[role]["running"]
    return result


@app.get("/api/live/dashboard")
def live_dashboard():
    """Very light operational snapshot for the overview page."""
    connection = connect()
    try:
        return {
            "parser": parser_health_snapshot(),
            "stats": light_stats_snapshot(connection),
            "refreshed_at": datetime.now(KYIV).strftime("%H:%M:%S"),
        }
    finally:
        connection.close()


ORIGINALS_ALLOWED_ROUTES = (
    "MUST_ANALYZE",
    "ANALYZE",
    "HOLD",
    "DROP_SOFT",
    "DROP_HARD",
    "DUPLICATE",
)
ORIGINALS_ROUTE_LABELS = {
    "MUST_ANALYZE": "Обов’язково аналізувати",
    "ANALYZE": "На аналіз",
    "HOLD": "Очікує сигналів",
    "DROP_SOFT": "Відсіяно",
    "DROP_HARD": "Технічний відсів",
    "DUPLICATE": "Дублікат",
}
ORIGINALS_EXPORT_MAX_ROWS = 10000


def _originals_route_values(values, legacy=""):
    result = []
    for value in [*(values or []), legacy]:
        normalized = str(value or "").strip().upper()
        if normalized in ORIGINALS_ALLOWED_ROUTES and normalized not in result:
            result.append(normalized)
    return result


def _originals_audience_values(values, legacy=""):
    result = []
    for value in [*(values or []), legacy]:
        normalized = str(value or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _originals_filter_parts(
    *,
    q="",
    date_from="",
    date_to="",
    source="",
    processing_status="all",
    routes=None,
    legacy_route="",
    audiences=None,
    legacy_audience="",
    canonical_only=False,
):
    conditions = ["1=1"]
    params = []

    query_text = str(q or "").strip()
    if query_text:
        if query_text.startswith("#") and len(query_text) > 1:
            conditions.append(
                "to_tsvector('simple',d.source_text) @@ plainto_tsquery('simple',?)"
            )
            params.append(query_text[1:])
        else:
            conditions.append(
                "to_tsvector('simple',COALESCE(NULLIF(d.full_text,''),d.source_text)) "
                "@@ plainto_tsquery('simple',?)"
            )
            params.append(query_text)

    if date_from:
        conditions.append(
            "COALESCE(NULLIF(d.published_at,'')::timestamptz,"
            "NULLIF(d.created_at,'')::timestamptz)>=?"
        )
        params.append(utc_boundary(date_from).isoformat())
    if date_to:
        conditions.append(
            "COALESCE(NULLIF(d.published_at,'')::timestamptz,"
            "NULLIF(d.created_at,'')::timestamptz)<?"
        )
        params.append((utc_boundary(date_to) + timedelta(days=1)).isoformat())

    source_text = str(source or "").strip()
    if source_text:
        conditions.append("d.source_title ILIKE ?")
        params.append(f"%{source_text}%")

    normalized_status = str(processing_status or "all").strip().lower()
    if normalized_status in {"processed", "prefilter_processed"}:
        normalized_status = "prefilter_processed"
        conditions.append("pf.source_document_id IS NOT NULL")
    elif normalized_status in {"unprocessed", "prefilter_unprocessed"}:
        normalized_status = "prefilter_unprocessed"
        conditions.append("pf.source_document_id IS NULL")
    elif normalized_status == "grok_processed":
        conditions.append("ga.source_document_id IS NOT NULL")
    elif normalized_status == "grok_unprocessed":
        conditions.append("ga.source_document_id IS NULL")
    else:
        normalized_status = "all"

    normalized_routes = _originals_route_values(routes, legacy_route)
    if normalized_routes:
        placeholders = ",".join("?" for _ in normalized_routes)
        conditions.append(f"pf.route IN ({placeholders})")
        params.extend(normalized_routes)

    normalized_audiences = _originals_audience_values(audiences, legacy_audience)
    if normalized_audiences:
        placeholders = ",".join("?" for _ in normalized_audiences)
        conditions.append(
            "EXISTS ("
            "SELECT 1 FROM jsonb_array_elements_text("
            "COALESCE(s.audiences,'[]'::jsonb)"
            ") AS aud(value) "
            f"WHERE aud.value IN ({placeholders})"
            ")"
        )
        params.extend(normalized_audiences)

    if canonical_only:
        conditions.append(
            "(pf.cluster_id IS NULL OR pf.canonical_document_id IS NULL "
            "OR pf.canonical_document_id=d.id)"
        )

    return (
        " AND ".join(conditions),
        params,
        normalized_status,
        normalized_routes,
        normalized_audiences,
    )


def _originals_pager_query(filters):
    pairs = [
        ("q", filters["q"]),
        ("date_from", filters["date_from"]),
        ("date_to", filters["date_to"]),
        ("source", filters["source"]),
        ("processing_status", filters["processing_status"]),
    ]
    for route_value in filters["routes"]:
        pairs.append(("routes", route_value))
    for audience_value in filters["audiences"]:
        pairs.append(("audiences", audience_value))
    if filters["canonical_only"]:
        pairs.append(("canonical_only", "true"))
    return urllib.parse.urlencode(pairs)


@app.get("/originals")
def originals(
    request: Request,
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    source: str = "",
    processing_status: str = "all",
    routes: list[str] | None = Query(default=None),
    route: str = "",
    audiences: list[str] | None = Query(default=None),
    audience: str = "",
    canonical_only: bool = False,
    page_no: int = Query(1, alias="page", ge=1),
):
    where, params, processing_status, route_values, audience_values = (
        _originals_filter_parts(
            q=q,
            date_from=date_from,
            date_to=date_to,
            source=source,
            processing_status=processing_status,
            routes=routes,
            legacy_route=route,
            audiences=audiences,
            legacy_audience=audience,
            canonical_only=canonical_only,
        )
    )

    page_size = 40
    offset = (page_no - 1) * page_size
    fast_default = (
        not str(q or "").strip()
        and not date_from
        and not date_to
        and not str(source or "").strip()
        and processing_status == "all"
        and not route_values
        and not audience_values
        and not canonical_only
    )
    connection = connect()
    try:
        if fast_default:
            total = int(
                connection.execute(
                    "SELECT count(*) AS total FROM source_documents"
                ).fetchone()["total"]
                or 0
            )
            rows_sql = """WITH picked AS (
                              SELECT id
                                FROM source_documents
                               ORDER BY id DESC
                               LIMIT ? OFFSET ?
                           )
                           SELECT d.id,d.published_at,d.created_at,d.source_title,d.source_url,
                                  d.source_kind,d.content_status,
                                  regexp_replace(
                                      left(COALESCE(NULLIF(d.feed_text,''),NULLIF(d.source_text,''),
                                                    NULLIF(d.full_text,''),''),620),
                                      E'[\n\r\t]+',' ','g'
                                  ) AS preview,
                                  COALESCE(s.audiences,'[]'::jsonb) AS audiences,
                                  (pf.source_document_id IS NOT NULL) AS prefilter_processed,
                                  pf.route AS prefilter_route,
                                  ga.decision AS grok_decision,
                                  pf.canonical_document_id,pf.cluster_id,
                                  COALESCE(cl.analysis_document_id,cl.canonical_document_id) AS analysis_document_id,
                                  COALESCE(cl.spread_count,1) AS spread_count,
                                  COALESCE(cl.unique_sources,1) AS unique_sources,
                                  COALESCE(cl.unique_domains,0) AS unique_domains,
                                  COALESCE(cl.unique_telegram_channels,0) AS unique_telegram_channels,
                                  COALESCE(cl.cross_platform,false) AS cross_platform
                             FROM picked x
                             JOIN source_documents d ON d.id=x.id
                        LEFT JOIN sources s ON s.id=d.source_id
                        LEFT JOIN info_prefilter_results_v1 pf ON pf.source_document_id=d.id
                        LEFT JOIN grok_document_analysis_v1 ga ON ga.source_document_id=d.id
                        LEFT JOIN primary_info_clusters_v1 cl ON cl.id=pf.cluster_id
                            ORDER BY d.id DESC"""
            rows_params = (page_size, offset)
        else:
            total = int(
                connection.execute(
                    f"""SELECT count(*) AS total
                          FROM source_documents d
                     LEFT JOIN sources s ON s.id=d.source_id
                     LEFT JOIN info_prefilter_results_v1 pf ON pf.source_document_id=d.id
                     LEFT JOIN grok_document_analysis_v1 ga ON ga.source_document_id=d.id
                         WHERE {where}""",
                    tuple(params),
                ).fetchone()["total"]
                or 0
            )
            rows_sql = f"""SELECT d.id,d.published_at,d.created_at,d.source_title,d.source_url,
                                  d.source_kind,d.content_status,
                                  regexp_replace(
                                      left(COALESCE(NULLIF(d.feed_text,''),NULLIF(d.source_text,''),
                                                    NULLIF(d.full_text,''),''),620),
                                      E'[\n\r\t]+',' ','g'
                                  ) AS preview,
                                  COALESCE(s.audiences,'[]'::jsonb) AS audiences,
                                  (pf.source_document_id IS NOT NULL) AS prefilter_processed,
                                  pf.route AS prefilter_route,
                                  ga.decision AS grok_decision,
                                  pf.canonical_document_id,pf.cluster_id,
                                  COALESCE(cl.analysis_document_id,cl.canonical_document_id) AS analysis_document_id,
                                  COALESCE(cl.spread_count,1) AS spread_count,
                                  COALESCE(cl.unique_sources,1) AS unique_sources,
                                  COALESCE(cl.unique_domains,0) AS unique_domains,
                                  COALESCE(cl.unique_telegram_channels,0) AS unique_telegram_channels,
                                  COALESCE(cl.cross_platform,false) AS cross_platform
                             FROM source_documents d
                        LEFT JOIN sources s ON s.id=d.source_id
                        LEFT JOIN info_prefilter_results_v1 pf ON pf.source_document_id=d.id
                        LEFT JOIN grok_document_analysis_v1 ga ON ga.source_document_id=d.id
                        LEFT JOIN primary_info_clusters_v1 cl ON cl.id=pf.cluster_id
                            WHERE {where}
                         ORDER BY COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                           NULLIF(d.created_at,'')::timestamptz) DESC NULLS LAST,
                                  d.id DESC
                            LIMIT ? OFFSET ?"""
            rows_params = tuple([*params, page_size, offset])

        rows = [dict(row) for row in connection.execute(rows_sql, rows_params).fetchall()]

        audience_options = [
            str(row["value"])
            for row in connection.execute(
                """SELECT value,count(*) AS sources
                     FROM sources s,
                          LATERAL jsonb_array_elements_text(
                              COALESCE(s.audiences,'[]'::jsonb)
                          ) x(value)
                    WHERE s.enabled
                    GROUP BY value
                    ORDER BY sources DESC,value"""
            ).fetchall()
        ]

        filters = {
            "q": q,
            "date_from": date_from,
            "date_to": date_to,
            "source": source,
            "processing_status": processing_status,
            "routes": route_values,
            "audiences": audience_values,
            "canonical_only": bool(canonical_only),
        }
        return page(
            request,
            "originals.html",
            rows=rows,
            total=total,
            page_no=page_no,
            page_size=page_size,
            filters=filters,
            pager_query=_originals_pager_query(filters),
            audience_options=audience_options,
            route_options=ORIGINALS_ALLOWED_ROUTES,
            route_labels=ORIGINALS_ROUTE_LABELS,
        )
    finally:
        connection.close()


class OriginalsExportRequest(BaseModel):
    mode: str = "filtered"
    format: str = "markdown"
    ids: list[int] = Field(default_factory=list)
    q: str = ""
    date_from: str = ""
    date_to: str = ""
    source: str = ""
    processing_status: str = "all"
    routes: list[str] = Field(default_factory=list)
    audiences: list[str] = Field(default_factory=list)
    canonical_only: bool = False


def _originals_export_row(row):
    item = dict(row)
    for key in (
        "actor_hits","narrative_hits","hashtag_hits","reason_codes","audiences",
        "grok_target_audiences","grok_mentioned_actors","grok_influence_methods",
        "grok_country_spaces",
    ):
        value = item.get(key)
        if value is None:
            item[key] = []
        elif not isinstance(value, list):
            item[key] = list(value) if isinstance(value, tuple) else value
    item["text"] = str(item.get("text") or "").replace("\x00", "")
    item["route_label"] = ORIGINALS_ROUTE_LABELS.get(
        str(item.get("route") or ""),
        str(item.get("route") or "Не оброблено"),
    )
    item["display_time"] = local_display(item.get("published_at") or item.get("created_at"))
    return item


def _originals_export_markdown(rows, payload):
    lines = [
        "# Пакет матеріалів «Оригінали»",
        "",
        f"- Створено: {datetime.now(KYIV).strftime('%d.%m.%Y %H:%M')}",
        f"- Матеріалів: {len(rows)}",
    ]
    if payload.date_from or payload.date_to:
        lines.append(
            f"- Період: {payload.date_from or '…'} — {payload.date_to or '…'}"
        )
    if payload.routes:
        lines.append(
            "- Маршрути: "
            + ", ".join(
                ORIGINALS_ROUTE_LABELS.get(route, route)
                for route in _originals_route_values(payload.routes)
            )
        )
    if payload.audiences:
        lines.append("- ЦА: " + ", ".join(payload.audiences))
    processing_labels = {
        "prefilter_processed": "первинно оброблено",
        "prefilter_unprocessed": "первинно не оброблено",
        "grok_processed": "Grok: оброблено",
        "grok_unprocessed": "Grok: не оброблено",
    }
    processing_key = str(payload.processing_status or "all").strip().lower()
    if processing_key in {"processed", "unprocessed"}:
        processing_key = "prefilter_" + processing_key
    if processing_key != "all":
        lines.append("- Обробка: " + processing_labels.get(processing_key, processing_key))
    if payload.canonical_only:
        lines.append("- Режим: лише оригінали сюжетів")
    lines.extend(["", "---", ""])

    for index, row in enumerate(rows, 1):
        title = (
            str(row.get("feed_text") or "").strip().splitlines()[0][:220]
            or str(row.get("text") or "").strip().splitlines()[0][:220]
            or f"Матеріал #{row['id']}"
        )
        lines.extend(
            [
                f"## {index}. {title}",
                "",
                f"- ID: {row['id']}",
                f"- Час: {row['display_time']}",
                f"- Джерело: {row.get('source_title') or 'Без назви'}",
                f"- Тип: {row.get('source_kind') or '—'}",
                f"- ЦА: {', '.join(row.get('audiences') or []) or '—'}",
                f"- Маршрут: {row['route_label']}",
                f"- Посилання: {row.get('source_url') or '—'}",
                (
                    f"- Поширення: {row.get('unique_sources') or 1} джерел · "
                    f"{row.get('spread_count') or 1} повторів · "
                    f"{row.get('unique_domains') or 0} доменів · "
                    f"{row.get('unique_telegram_channels') or 0} Telegram-каналів"
                ),
            ]
        )
        if row.get("actor_hits"):
            lines.append("- Актори: " + ", ".join(map(str, row["actor_hits"])))
        if row.get("narrative_hits"):
            lines.append("- Наративні сигнали: " + ", ".join(map(str, row["narrative_hits"])))
        if row.get("hashtag_hits"):
            lines.append("- Теги: " + ", ".join(map(str, row["hashtag_hits"])))
        if row.get("reason_codes"):
            lines.append("- Причини маршруту: " + ", ".join(map(str, row["reason_codes"])))
        if row.get("grok_decision"):
            lines.extend(["", "### Аналіз Grok", ""])
            lines.append(
                f"- Рішення: {row.get('grok_decision')} · "
                f"пріоритет {row.get('grok_priority') or '—'} · "
                f"confidence {row.get('grok_confidence') if row.get('grok_confidence') is not None else '—'}"
            )
            lines.append(
                f"- Спікер: {row.get('grok_speaker') or '—'}"
                + (f" ({row.get('grok_speaker_role')})" if row.get("grok_speaker_role") else "")
            )
            if row.get("grok_information_trigger"):
                lines.append("- Інфопривід: " + str(row["grok_information_trigger"]))
            if row.get("grok_main_claim"):
                lines.append("- Головна теза: " + str(row["grok_main_claim"]))
            if row.get("grok_framing"):
                lines.append("- Фреймінг: " + str(row["grok_framing"]))
            if row.get("grok_story_relation"):
                story_line = "- Story: " + str(row["grok_story_relation"])
                if row.get("grok_story_key"):
                    story_line += " · " + str(row["grok_story_key"])
                lines.append(story_line)
            if row.get("grok_target_audiences"):
                lines.append("- ЦА Grok: " + ", ".join(map(str, row["grok_target_audiences"])))
            if row.get("grok_country_spaces"):
                lines.append("- Країни/простори: " + ", ".join(map(str, row["grok_country_spaces"])))
            if row.get("grok_influence_methods"):
                lines.append("- Методи впливу: " + ", ".join(map(str, row["grok_influence_methods"])))
            if row.get("grok_expected_effect"):
                lines.append("- Очікуваний ефект: " + str(row["grok_expected_effect"]))
            if row.get("grok_added_value"):
                lines.append("- Додана цінність: " + str(row["grok_added_value"]))
            if row.get("grok_reason"):
                lines.append("- Evidence/підстава: " + str(row["grok_reason"]))
        lines.extend(["", "### Текст", "", row.get("text") or "—", "", "---", ""])
    return "\n".join(lines)


@app.post("/api/originals/export", dependencies=[Depends(require_action_token)])
def export_originals(payload: OriginalsExportRequest):
    mode = str(payload.mode or "filtered").strip().lower()
    export_format = str(payload.format or "markdown").strip().lower()
    if mode not in {"filtered","selected"}:
        raise HTTPException(400, "Невідомий режим вивантаження")
    if export_format not in {"markdown","json","csv"}:
        raise HTTPException(400, "Підтримуються Markdown, JSON або CSV")

    connection = connect()
    try:
        params = []
        if mode == "selected":
            selected_ids = []
            for value in payload.ids:
                try:
                    item_id = int(value)
                except (TypeError, ValueError):
                    continue
                if item_id > 0 and item_id not in selected_ids:
                    selected_ids.append(item_id)
            if not selected_ids:
                raise HTTPException(400, "Не вибрано жодного матеріалу")
            if len(selected_ids) > ORIGINALS_EXPORT_MAX_ROWS:
                raise HTTPException(
                    413,
                    f"За один раз можна вивантажити до {ORIGINALS_EXPORT_MAX_ROWS} матеріалів",
                )
            placeholders = ",".join("?" for _ in selected_ids)
            where = f"d.id IN ({placeholders})"
            params = selected_ids
        else:
            where, params, _, _, _ = _originals_filter_parts(
                q=payload.q,
                date_from=payload.date_from,
                date_to=payload.date_to,
                source=payload.source,
                processing_status=payload.processing_status,
                routes=payload.routes,
                audiences=payload.audiences,
                canonical_only=payload.canonical_only,
            )
            count_row = connection.execute(
                f"""SELECT count(*) AS total
                      FROM source_documents d
                 LEFT JOIN sources s ON s.id=d.source_id
                 LEFT JOIN info_prefilter_results_v1 pf ON pf.source_document_id=d.id
                 LEFT JOIN grok_document_analysis_v1 ga ON ga.source_document_id=d.id
                     WHERE {where}""",
                tuple(params),
            ).fetchone()
            total = int(count_row["total"] or 0)
            if total > ORIGINALS_EXPORT_MAX_ROWS:
                raise HTTPException(
                    413,
                    (
                        f"За фільтрами знайдено {total} матеріалів. "
                        f"Ліміт одного файлу — {ORIGINALS_EXPORT_MAX_ROWS}. "
                        "Звузьте період, маршрути або увімкніть «лише оригінали сюжетів»."
                    ),
                )

        rows = [
            _originals_export_row(row)
            for row in connection.execute(
                f"""SELECT d.id,d.published_at,d.created_at,d.source_title,d.source_url,
                           d.source_kind,d.feed_text,
                           COALESCE(NULLIF(d.full_text,''),NULLIF(d.source_text,''),
                                    NULLIF(d.feed_text,''),'') AS text,
                           COALESCE(s.audiences,'[]'::jsonb) AS audiences,
                           pf.route,
                           pf.domain_score,pf.influence_score,pf.spread_score,
                           pf.actor_hits,pf.narrative_hits,pf.hashtag_hits,pf.reason_codes,
                           pf.cluster_id,pf.canonical_document_id,
                           ga.decision AS grok_decision,
                           ga.priority AS grok_priority,
                           ga.speaker AS grok_speaker,
                           ga.speaker_role AS grok_speaker_role,
                           ga.information_trigger AS grok_information_trigger,
                           ga.main_claim AS grok_main_claim,
                           ga.framing AS grok_framing,
                           ga.story_relation AS grok_story_relation,
                           ga.story_key AS grok_story_key,
                           ga.confidence AS grok_confidence,
                           ga.expected_effect AS grok_expected_effect,
                           ga.added_value AS grok_added_value,
                           ga.reason AS grok_reason,
                           ga.target_audiences AS grok_target_audiences,
                           ga.mentioned_actors AS grok_mentioned_actors,
                           ga.influence_methods AS grok_influence_methods,
                           ga.country_spaces AS grok_country_spaces,
                           COALESCE(cl.spread_count,1) AS spread_count,
                           COALESCE(cl.unique_sources,1) AS unique_sources,
                           COALESCE(cl.unique_domains,0) AS unique_domains,
                           COALESCE(cl.unique_telegram_channels,0) AS unique_telegram_channels,
                           COALESCE(cl.cross_platform,false) AS cross_platform
                      FROM source_documents d
                 LEFT JOIN sources s ON s.id=d.source_id
                 LEFT JOIN info_prefilter_results_v1 pf ON pf.source_document_id=d.id
                 LEFT JOIN grok_document_analysis_v1 ga ON ga.source_document_id=d.id
                 LEFT JOIN primary_info_clusters_v1 cl ON cl.id=pf.cluster_id
                     WHERE {where}
                  ORDER BY COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                    NULLIF(d.created_at,'')::timestamptz) ASC NULLS LAST,
                           d.id ASC
                     LIMIT ?""",
                tuple([*params, ORIGINALS_EXPORT_MAX_ROWS]),
            ).fetchall()
        ]
    finally:
        connection.close()

    stamp = datetime.now(KYIV).strftime("%Y%m%d-%H%M%S")
    if export_format == "json":
        body = json.dumps(rows, ensure_ascii=False, indent=2, default=str)
        media_type = "application/json; charset=utf-8"
        filename = f"originals-export-{stamp}.json"
    elif export_format == "csv":
        output = io.StringIO()
        columns = [
            "id","display_time","source_title","source_kind","audiences","route_label",
            "source_url","unique_sources","spread_count","unique_domains",
            "unique_telegram_channels","actor_hits","narrative_hits","hashtag_hits",
            "reason_codes","text",
        ]
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            csv_row = dict(row)
            for key in ("audiences","actor_hits","narrative_hits","hashtag_hits","reason_codes"):
                value = csv_row.get(key) or []
                csv_row[key] = "; ".join(map(str, value)) if isinstance(value, list) else str(value)
            writer.writerow(csv_row)
        body = "\ufeff" + output.getvalue()
        media_type = "text/csv; charset=utf-8"
        filename = f"originals-export-{stamp}.csv"
    else:
        body = _originals_export_markdown(rows, payload)
        media_type = "text/markdown; charset=utf-8"
        filename = f"originals-export-{stamp}.md"

    return Response(
        body,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Export-Count": str(len(rows)),
        },
    )


@app.get("/downloads/grok-test-50.jsonl")
def download_grok_test_50():
    export_path = ROOT / "_patch_backups" / "grok_test_50.jsonl"
    if not export_path.exists():
        raise HTTPException(404, "Тестовий пакет ще не сформовано")
    return Response(
        export_path.read_bytes(),
        media_type="application/x-ndjson; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="grok-test-50.jsonl"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/downloads/grok-test-100-v14.jsonl")
def download_grok_test_100_v14():
    export_path = ROOT / "_patch_backups" / "grok_test_100_v14.jsonl"
    if not export_path.exists():
        raise HTTPException(404, "Тестовий пакет ще не сформовано")
    return Response(
        export_path.read_bytes(),
        media_type="application/x-ndjson; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="grok-test-100-v14.jsonl"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/downloads/grok-test-100-v14-prompt.md")
def download_grok_test_100_v14_prompt():
    export_path = ROOT / "_patch_backups" / "grok_test_100_v14_prompt.md"
    if not export_path.exists():
        raise HTTPException(404, "Інструкцію ще не сформовано")
    return Response(
        export_path.read_bytes(),
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="grok-test-100-v14-prompt.md"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/originals/{document_id}")
def original_detail(request: Request, document_id: int):
    connection = connect()
    try:
        row = connection.execute(
            """SELECT d.*,
                      (pf.source_document_id IS NOT NULL) AS prefilter_processed,
                      pf.route AS prefilter_route,
                      pf.domain_score,pf.influence_score,pf.spread_score,
                      pf.actor_hits,pf.narrative_hits,pf.hashtag_hits,pf.reason_codes,
                      pf.rules_version,pf.cluster_id,pf.canonical_document_id,
                      COALESCE(cl.analysis_document_id,cl.canonical_document_id) AS analysis_document_id,
                      COALESCE(cl.spread_count,1) AS spread_count,
                      COALESCE(cl.unique_sources,1) AS unique_sources,
                      COALESCE(cl.unique_domains,0) AS unique_domains,
                      COALESCE(cl.unique_telegram_channels,0) AS unique_telegram_channels,
                      COALESCE(cl.cross_platform,false) AS cross_platform,
                      cl.first_seen_at,cl.last_seen_at
                 FROM source_documents d
            LEFT JOIN info_prefilter_results_v1 pf ON pf.source_document_id=d.id
            LEFT JOIN primary_info_clusters_v1 cl ON cl.id=pf.cluster_id
                WHERE d.id=?""",
            (document_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404,"Оригінал не знайдено")
        versions = connection.execute(
            "SELECT sha256,origin,left(source_text,300) AS preview FROM original_versions WHERE source_document_id=?",
            (document_id,),
        ).fetchall()
        translation = connection.execute(
            "SELECT * FROM document_translations WHERE source_document_id=?",
            (document_id,),
        ).fetchone()
        telegram_markers = connection.execute(
            """SELECT marker_type,marker,normalized_marker,marker_index
                 FROM telegram_document_markers_v1
                WHERE source_document_id=?
                ORDER BY CASE marker_type WHEN 'hashtag' THEN 0 ELSE 1 END,marker_index,normalized_marker""",
            (document_id,),
        ).fetchall()
        spread_members = []
        dzen_spread = []
        if row["cluster_id"]:
            spread_members = connection.execute(
                """SELECT d.id,d.published_at,d.source_title,d.source_url,d.source_kind,
                          m.match_type,m.similarity,m.propagation_role,
                          (d.id=?) AS is_canonical
                     FROM primary_info_cluster_members_v1 m
                     JOIN source_documents d ON d.id=m.source_document_id
                    WHERE m.cluster_id=?
                    ORDER BY COALESCE(NULLIF(d.published_at,'')::timestamptz,
                                      NULLIF(d.created_at,'')::timestamptz),d.id
                    LIMIT 250""",
                (row["canonical_document_id"], row["cluster_id"]),
            ).fetchall()
            if db.has_table(connection,"dzen_story_matches_v1"):
                dzen_spread = connection.execute(
                    """SELECT v.dzen_cluster_id,v.dzen_title,v.dzen_source_count,
                              v.discovery_score,v.publication_at,v.local_match_count,
                              v.match_confidence,s.story_url,s.representative_source
                         FROM dzen_primary_spread_v1 v
                         JOIN dzen_stories_v1 s ON s.cluster_id=v.dzen_cluster_id
                        WHERE v.primary_cluster_id=?
                        ORDER BY v.dzen_source_count DESC,v.discovery_score DESC,
                                 v.publication_at DESC NULLS LAST
                        LIMIT 50""",
                    (row["cluster_id"],),
                ).fetchall()
        return page(
            request,"original_detail.html",item=row,versions=versions,
            translation=translation,spread_members=spread_members,dzen_spread=dzen_spread,
            telegram_markers=telegram_markers,
        )
    finally:
        connection.close()


@app.get("/sources")
def source_list(
    request: Request, q: str = "", status: str = "all",
    kind: str = "all", enabled: str = "all",
):
    connection = connect()
    try:
        rows = [dict(row) for row in connection.execute("""
            WITH ep AS (
              SELECT e.source_id,
                     count(*) FILTER (WHERE e.enabled) AS endpoint_count,
                     count(*) FILTER (WHERE h.checked_at IS NOT NULL) AS checked_count,
                     count(*) FILTER (WHERE h.check_ok) AS ok_count,
                     count(*) FILTER (WHERE h.checked_at IS NOT NULL AND NOT h.check_ok) AS bad_count,
                     max(h.checked_at) AS checked_at,
                     sum(COALESCE(h.missing_count,0)) AS missing_count,
                     min(e.endpoint) FILTER (WHERE e.enabled) AS primary_endpoint,
                     CASE WHEN bool_or(e.kind='telegram') THEN 'telegram'
                          WHEN bool_or(e.kind='rss') THEN 'rss' ELSE 'web' END AS endpoint_kind
                FROM source_endpoints e
                LEFT JOIN source_health h ON h.endpoint=e.endpoint
               GROUP BY e.source_id
            ),
            docs AS (
              SELECT d.source_id,
                     max(NULLIF(d.created_at,'')::timestamptz) AS last_database_at,
                     count(*) FILTER (WHERE NULLIF(d.created_at,'')::timestamptz>=now()-interval '24 hours') AS docs24,
                     count(*) FILTER (WHERE NULLIF(d.created_at,'')::timestamptz>=now()-interval '7 days') AS docs7,
                     count(*) FILTER (WHERE NULLIF(d.created_at,'')::timestamptz>=now()-interval '24 hours'
                                      AND p.route='MUST_ANALYZE') AS must24,
                     count(*) FILTER (WHERE NULLIF(d.created_at,'')::timestamptz>=now()-interval '24 hours'
                                      AND p.route='ANALYZE') AS analyze24,
                     count(*) FILTER (WHERE NULLIF(d.created_at,'')::timestamptz>=now()-interval '24 hours'
                                      AND p.route='HOLD') AS hold24,
                     count(*) FILTER (WHERE NULLIF(d.created_at,'')::timestamptz>=now()-interval '24 hours'
                                      AND p.route IN ('DROP_SOFT','DROP_HARD')) AS drop24
                FROM source_documents d
                LEFT JOIN info_prefilter_results_v1 p ON p.source_document_id=d.id
               GROUP BY d.source_id
            )
            SELECT s.*,COALESCE(ep.endpoint_count,0) endpoint_count,
                   COALESCE(ep.checked_count,0) checked_count,
                   COALESCE(ep.ok_count,0) ok_count,COALESCE(ep.bad_count,0) bad_count,
                   ep.checked_at,COALESCE(ep.missing_count,0) missing_count,
                   ep.primary_endpoint,COALESCE(ep.endpoint_kind,s.kind) endpoint_kind,
                   docs.last_database_at,COALESCE(docs.docs24,0) docs24,
                   COALESCE(docs.docs7,0) docs7,COALESCE(docs.must24,0) must24,
                   COALESCE(docs.analyze24,0) analyze24,COALESCE(docs.hold24,0) hold24,
                   COALESCE(docs.drop24,0) drop24
              FROM sources s
              LEFT JOIN ep ON ep.source_id=s.id
              LEFT JOIN docs ON docs.source_id=s.id
             ORDER BY s.title,s.id
        """).fetchall()]

        def decorate(row):
            if not row["enabled"]:
                state,label,badge="disabled","Вимкнено",""
            elif int(row["docs24"] or 0)>0:
                state,label,badge="healthy","Працює","green"
            elif row["last_database_at"] is None:
                state,label,badge="never","Немає даних","red"
            elif int(row["docs7"] or 0)==0:
                state,label,badge="problem","Без даних 7д","red"
            else:
                state,label,badge="quiet","Тихе >24г","amber"
            row["health_state"],row["health_label"],row["health_badge"]=state,label,badge
            return row

        rows=[decorate(row) for row in rows]
        summary={
            "total":len(rows),
            "healthy":sum(r["health_state"]=="healthy" for r in rows),
            "quiet":sum(r["health_state"]=="quiet" for r in rows),
            "problem":sum(r["health_state"]=="problem" for r in rows),
            "never":sum(r["health_state"]=="never" for r in rows),
            "endpoint_errors":sum(int(r["bad_count"] or 0) for r in rows),
        }
        dzen_summary={"total":0,"registered":0,"unregistered":0,"active24":0}
        dzen_candidates=[]
        if db.has_table(connection,"dzen_source_candidates_v1"):
            dzen_summary=dict(connection.execute("""
                SELECT count(*)::int total,
                       count(*) FILTER (WHERE matched_source_id IS NOT NULL)::int registered,
                       count(*) FILTER (WHERE matched_source_id IS NULL)::int unregistered,
                       count(*) FILTER (WHERE stories_24h>0)::int active24
                  FROM dzen_source_candidates_v1
            """).fetchone())
            dzen_candidates=[dict(r) for r in connection.execute("""
                SELECT source_name,sample_domain,sample_article_url,publications_24h,
                       stories_24h,relevant_stories_24h,last_seen_at,source_match_confidence
                  FROM dzen_source_candidates_v1
                 WHERE matched_source_id IS NULL AND stories_24h>0
                 ORDER BY relevant_stories_24h DESC,stories_24h DESC,publications_24h DESC
                 LIMIT 20
            """).fetchall()]
        needle=q.strip().lower()
        if needle:
            rows=[r for r in rows if needle in (r["title"] or "").lower()
                  or needle in (r["url"] or "").lower()
                  or needle in (r["source_key"] or "").lower()
                  or needle in (r["primary_endpoint"] or "").lower()]
        if status in {"healthy","quiet","problem","never","disabled"}:
            rows=[r for r in rows if r["health_state"]==status]
        if kind in {"telegram","rss","web"}:
            rows=[r for r in rows if r["endpoint_kind"]==kind]
        if enabled in {"yes","no"}:
            flag=enabled=="yes"
            rows=[r for r in rows if bool(r["enabled"])==flag]
        return page(
            request,"sources.html",rows=rows,summary=summary,
            filters=dict(q=q,status=status,kind=kind,enabled=enabled),
            dzen_summary=dzen_summary,dzen_candidates=dzen_candidates,
        )
    finally:
        connection.close()


@app.get("/sources/{source_id}")
def source_detail(request: Request, source_id: int):
    connection=connect()
    try:
        item=connection.execute("SELECT * FROM sources WHERE id=?",(source_id,)).fetchone()
        if not item:
            raise HTTPException(404,"Джерело не знайдено")
        endpoints=connection.execute("""
            SELECT e.*,h.checked_at,h.check_ok,h.remote_item_at,h.remote_item_key,
                   h.missing_count,h.error AS health_error,h.details
              FROM source_endpoints e
              LEFT JOIN source_health h ON h.endpoint=e.endpoint
             WHERE e.source_id=? ORDER BY e.enabled DESC,e.endpoint
        """,(source_id,)).fetchall()
        stats=dict(connection.execute("""
            SELECT count(*) FILTER(WHERE NULLIF(d.created_at,'')::timestamptz>=now()-interval '24 hours') docs24,
                   count(*) FILTER(WHERE NULLIF(d.created_at,'')::timestamptz>=now()-interval '7 days') docs7,
                   max(NULLIF(d.created_at,'')::timestamptz) last_database_at,
                   count(*) FILTER(WHERE NULLIF(d.created_at,'')::timestamptz>=now()-interval '7 days' AND p.route='MUST_ANALYZE') must7,
                   count(*) FILTER(WHERE NULLIF(d.created_at,'')::timestamptz>=now()-interval '7 days' AND p.route='ANALYZE') analyze7,
                   count(*) FILTER(WHERE NULLIF(d.created_at,'')::timestamptz>=now()-interval '7 days' AND p.route='HOLD') hold7,
                   count(*) FILTER(WHERE NULLIF(d.created_at,'')::timestamptz>=now()-interval '7 days'
                                    AND p.route IN ('DROP_SOFT','DROP_HARD')) drop7
              FROM source_documents d
              LEFT JOIN info_prefilter_results_v1 p ON p.source_document_id=d.id
             WHERE d.source_id=?
        """,(source_id,)).fetchone())
        recent=connection.execute("""
            SELECT d.id,d.created_at,d.published_at,d.feed_text,d.source_url,
                   p.route,p.domain_score,p.influence_score
              FROM source_documents d
              LEFT JOIN info_prefilter_results_v1 p ON p.source_document_id=d.id
             WHERE d.source_id=?
             ORDER BY NULLIF(d.created_at,'')::timestamptz DESC NULLS LAST,d.id DESC
             LIMIT 25
        """,(source_id,)).fetchall()
        telegram_markers=connection.execute("""
            SELECT m.marker_type,m.normalized_marker,min(m.marker) AS marker,
                   count(*)::int AS uses,count(DISTINCT m.source_document_id)::int AS documents
              FROM telegram_document_markers_v1 m
              JOIN source_documents d ON d.id=m.source_document_id
             WHERE m.source_id=? AND NULLIF(d.created_at,'')::timestamptz>=now()-interval '7 days'
             GROUP BY m.marker_type,m.normalized_marker
             ORDER BY CASE m.marker_type WHEN 'hashtag' THEN 0 ELSE 1 END,uses DESC,m.normalized_marker
             LIMIT 120
        """,(source_id,)).fetchall()
        return page(request,"source_detail.html",item=item,endpoints=endpoints,stats=stats,recent=recent,telegram_markers=telegram_markers)
    finally:
        connection.close()

@app.get("/database")
def database_page(request: Request):
    connection = connect()
    try:
        counts = dict(connection.execute("""SELECT
          (SELECT count(*) FROM source_documents) AS originals,
          (SELECT count(*) FROM info_influence) AS influence,
          (SELECT count(*) FROM news) AS notes,
          (SELECT count(*) FROM subscribers WHERE is_active=1) AS active_subscribers,
          (SELECT count(*) FROM telegram_deliveries WHERE deleted_at IS NULL) AS deliveries""").fetchone())
        counts.update(collector_queue_snapshot(connection))
        subscribers = connection.execute(
            """SELECT s.*,(SELECT count(*) FROM telegram_deliveries d WHERE d.chat_id=s.chat_id) AS deliveries,
                      (SELECT max(sent_at) FROM telegram_deliveries d WHERE d.chat_id=s.chat_id) AS last_delivery
               FROM subscribers s ORDER BY is_active DESC,updated_at DESC"""
        ).fetchall()
        max_age_hours = max(1, int(os.getenv("RECOVERY_MAX_AGE_HOURS", "48")))
        classified = connection.execute(
            f"""SELECT d.id,d.published_at,d.source_title,left(COALESCE(NULLIF(d.full_text,''),d.source_text),300) AS preview,
                      d.metadata,
                      CASE WHEN i.id IS NULL THEN NULL
                           WHEN length(trim(COALESCE(i.status,'')))>0 THEN trim(i.status)
                           WHEN length(trim(COALESCE(i.narrative_title,'')))>0
                             OR i.role IN ('carrier','amplifier','reporting','trigger')
                             THEN 'accepted'
                           ELSE 'rejected' END AS info_status,
                      i.narrative_title,i.actors,i.claims,
                      i.techniques,i.strategic_objectives,
                      COALESCE(NULLIF(d.published_at,'')::timestamptz,
                               NULLIF(d.created_at,'')::timestamptz)
                        >=now()-({max_age_hours}||' hours')::interval AS recovery_eligible
               FROM source_documents d
               JOIN sources s ON s.id=d.source_id
               LEFT JOIN info_influence i ON i.source_document_id=d.id
               WHERE (s.enabled AND s.for_info) OR i.id IS NOT NULL
               ORDER BY d.id DESC LIMIT 50"""
        ).fetchall()
        return page(request,"database.html",counts=counts,subscribers=subscribers,classified=classified)
    finally:
        connection.close()


@app.get("/influence")
def influence_page(
    request: Request, period: str = "all", audience: str = "", speaker: str = "",
    direction: str = "", narrative: str = "", date_from: str = "", date_to: str = "",
):
    if period not in {"day", "48h", "week", "all", "custom"}:
        period = "all"
    connection = connect()
    try:
        try:
            data = influence_dashboard.build(
                connection, period=period, audience=audience.strip(), speaker=speaker.strip(),
                direction=direction.strip(), narrative=narrative.strip(),
                date_from=date_from, date_to=date_to,
            )
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        return page(request, "influence.html", **data)
    finally:
        connection.close()


@app.get("/search")
def statement_search(request: Request, q: str = "", date_from: str = "", date_to: str = "",
                     direction: str = "all", source: str = ""):
    conditions, params = ["1=1"], []
    if q.strip():
        needle=f"%{q.strip()}%"
        conditions.append("(COALESCE(NULLIF(d.full_text,''),d.source_text) ILIKE ? OR COALESCE(i.actors,'[]')::text ILIKE ? OR COALESCE(i.claims,'[]')::text ILIKE ? OR i.narrative_title ILIKE ? OR i.summary ILIKE ? OR COALESCE(i.strategic_objectives,'[]')::text ILIKE ? OR COALESCE(i.techniques,'[]')::text ILIKE ?)")
        params.extend([needle]*7)
    if date_from:
        conditions.append("NULLIF(d.published_at,'')::timestamptz>=?"); params.append(utc_boundary(date_from))
    if date_to:
        conditions.append("NULLIF(d.published_at,'')::timestamptz<?"); params.append(utc_boundary(date_to)+timedelta(days=1))
    if source.strip():
        conditions.append("d.source_title ILIKE ?"); params.append(f"%{source.strip()}%")
    if direction == "br": conditions.append("s.for_br")
    if direction == "info": conditions.append("s.for_info")
    connection=connect()
    try:
        rows=connection.execute(
            f"""SELECT d.id,d.published_at,d.source_title,d.source_url,
                       left(COALESCE(NULLIF(d.full_text,''),d.source_text),700) AS preview,
                       s.for_br,s.for_info,i.status AS influence_status,i.narrative_title,i.actors,i.techniques
                  FROM source_documents d LEFT JOIN sources s ON s.id=d.source_id
                  LEFT JOIN info_influence i ON i.source_document_id=d.id
                 WHERE {' AND '.join(conditions)}
                 ORDER BY NULLIF(d.published_at,'')::timestamptz DESC NULLS LAST,d.id DESC LIMIT 500""",
            tuple(params),
        ).fetchall()
        return page(request,"search.html",rows=rows,filters=dict(q=q,date_from=date_from,date_to=date_to,direction=direction,source=source))
    finally:
        connection.close()


@app.get("/queues")
def queues(request: Request):
    connection = connect()
    try:
        collector_counts = collector_queue_snapshot(connection)
        maximum = max(1, int(os.getenv("COLLECTOR_MAX_ATTEMPTS", "3")))
        note_pending_total = connection.execute(
            "SELECT count(*) FROM note_drafts WHERE status IN ('queued','processing','manual_review','failed')"
        ).fetchone()[0]
        collector_attention = connection.execute(
            f"""SELECT d.id,d.published_at,d.source_title,d.source_url,
                       m.status AS br_status,d.br_attempts,d.br_last_error,
                       i.status AS info_status,d.info_attempts,d.info_last_error
                  FROM source_documents d JOIN sources s ON s.id=d.source_id
                  LEFT JOIN messages m
                    ON m.chat_id=d.source_chat_id AND m.message_id=d.source_message_id
                  LEFT JOIN info_influence i ON i.source_document_id=d.id
                 WHERE s.enabled AND (
                   (s.for_br AND m.status='error' AND d.br_attempts>={maximum}) OR
                   (s.for_info AND i.status='error' AND d.info_attempts>={maximum}))
                 ORDER BY d.updated_at DESC,d.id DESC LIMIT 100"""
        ).fetchall()
        notes = connection.execute(
            """SELECT n.*,left(d.source_text,300) AS original_preview
               FROM note_drafts n LEFT JOIN source_documents d ON d.id=n.source_document_id
               WHERE n.status IN ('queued','processing','manual_review','failed')
               ORDER BY CASE n.status WHEN 'manual_review' THEN 0 WHEN 'failed' THEN 1 WHEN 'queued' THEN 2 ELSE 3 END,
                        n.created_at DESC LIMIT 100"""
        ).fetchall()
        events = connection.execute(
            """SELECT e.*,d.source_title,d.source_url,left(d.source_text,300) AS original_preview
               FROM event_candidates e LEFT JOIN source_documents d ON d.id=e.source_document_id
               WHERE e.status IN ('queued','processing','manual_review','failed')
               ORDER BY CASE e.status WHEN 'manual_review' THEN 0 WHEN 'failed' THEN 1 WHEN 'queued' THEN 2 ELSE 3 END,
                        e.created_at DESC LIMIT 100"""
        ).fetchall()
        return page(
            request,"queues.html",notes=notes,events=events,
            collector_counts=collector_counts,
            collector_attention=collector_attention,
            note_pending_total=note_pending_total,
            collector_max_attempts=maximum,
        )
    finally:
        connection.close()


@app.get("/calendar")
def calendar(request: Request, date_from: str = "", date_to: str = "", status: str = "all"):
    conditions, params = ["1=1"], []
    if date_from: conditions.append("(e.starts_at>=? OR e.starts_at IS NULL)"); params.append(utc_boundary(date_from))
    if date_to: conditions.append("(e.starts_at<? OR e.starts_at IS NULL)"); params.append(utc_boundary(date_to)+timedelta(days=1))
    if status != "all": conditions.append("e.status=?"); params.append(status)
    connection = connect()
    try:
        rows = [dict(row) for row in connection.execute(
            f"""SELECT e.*,(SELECT count(*) FROM calendar_event_sources s WHERE s.event_id=e.id) AS source_count,
                        (SELECT count(*) FROM calendar_event_revisions r WHERE r.event_id=e.id) AS revision_count
                 FROM calendar_events e WHERE {' AND '.join(conditions)}
                 ORDER BY e.starts_at NULLS LAST,e.updated_at DESC LIMIT 300""",
            tuple(params),
        ).fetchall()]
        hints = {row["id"]: row for row in calendar_dates.preview_unknown_dates(connection)}
        for row in rows:
            row["date_hint"] = hints.get(row["id"])
            legacy_summary = " ".join((row.get("summary") or "").replace("#подія", "").split())
            clean_title = " ".join((row.get("title") or "").replace("#подія", "").split())
            row["display_summary"] = "" if legacy_summary == clean_title else row.get("summary", "")
        return page(request,"calendar.html",rows=rows,date_hint_count=len(hints),filters=dict(date_from=date_from,date_to=date_to,status=status))
    finally:
        connection.close()


@app.get("/calendar/{event_id}")
def calendar_detail(request: Request, event_id: int):
    connection = connect()
    try:
        item = connection.execute("SELECT * FROM calendar_events WHERE id=?",(event_id,)).fetchone()
        if not item: raise HTTPException(404,"Подію не знайдено")
        sources = connection.execute("SELECT * FROM calendar_event_sources WHERE event_id=? ORDER BY observed_at DESC",(event_id,)).fetchall()
        revisions = connection.execute("SELECT * FROM calendar_event_revisions WHERE event_id=? ORDER BY revision_no DESC",(event_id,)).fetchall()
        return page(request,"calendar_detail.html",item=item,sources=sources,revisions=revisions)
    finally: connection.close()


@app.get("/notes")
def notes(request: Request, q: str = "", current: str = "yes"):
    conditions, params = ["1=1"], []
    if q.strip():
        conditions.append("(paragraph ILIKE ? OR source_title ILIKE ? OR event_key ILIKE ?)")
        params.extend([f"%{q.strip()}%"]*3)
    if current == "yes": conditions.append("is_current=1")
    connection = connect()
    try:
        rows = connection.execute(
            f"""SELECT n.*,(SELECT count(*) FROM telegram_deliveries d WHERE d.news_id=n.id AND d.deleted_at IS NULL) AS deliveries
                 FROM news n WHERE {' AND '.join(conditions)} ORDER BY updated_at DESC LIMIT 200""",tuple(params)
        ).fetchall()
        return page(request,"notes.html",rows=rows,filters=dict(q=q,current=current))
    finally: connection.close()


@app.get("/analysis")
def analysis_page(request: Request):
    connection = connect()
    try:
        jobs = connection.execute("SELECT * FROM analysis_jobs ORDER BY id DESC LIMIT 100").fetchall()
        return page(request,"analysis.html",jobs=jobs)
    finally: connection.close()


@app.get("/models")
def model_page(request: Request):
    connection = connect()
    try:
        usage = connection.execute(
            """SELECT role,stage,model,count(*) AS calls,sum(input_chars) AS input_chars,
                      sum(output_chars) AS output_chars,round(avg(duration_ms)/1000.0,1) AS avg_seconds,
                      count(*) FILTER(WHERE NOT success) AS errors
               FROM model_usage GROUP BY role,stage,model ORDER BY role,stage,model"""
        ).fetchall()
        return page(request,"models.html",usage=usage,workloads=workload_snapshot(connection))
    finally: connection.close()


@app.get("/logs")
def logs(request: Request, role: str = "collector"):
    if role not in process_manager.SPECS: role="collector"
    return page(request,"logs.html",role=role,content=process_manager.tail(role),processes=process_manager.all_statuses())


@app.get("/logs/diagnostics/download")
def download_diagnostics():
    connection=connect()
    try:
        diagnostic={
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "processes": process_manager.all_statuses(),
            "logs": {role: process_manager.raw_tail(role) for role in process_manager.SPECS},
            "failed_model_calls": [dict(row) for row in connection.execute(
                "SELECT role,stage,model,error,created_at FROM model_usage WHERE NOT success ORDER BY id DESC LIMIT 100"
            ).fetchall()],
            "failed_analysis": [dict(row) for row in connection.execute(
                "SELECT id,job_type,prompt,status,error,created_at FROM analysis_jobs WHERE status='failed' ORDER BY id DESC LIMIT 50"
            ).fetchall()],
            "queue_errors": [dict(row) for row in connection.execute(
                """SELECT 'note' AS kind,id,status,error,created_at FROM note_drafts WHERE error<>''
                   UNION ALL SELECT 'event',id,status,error,created_at FROM event_candidates WHERE error<>''
                   ORDER BY created_at DESC LIMIT 100"""
            ).fetchall()],
            "source_errors": [dict(row) for row in connection.execute(
                "SELECT endpoint,checked_at,error FROM source_health WHERE error<>'' ORDER BY checked_at DESC LIMIT 100"
            ).fetchall()],
        }
    finally:
        connection.close()
    body=json.dumps(diagnostic,ensure_ascii=False,indent=2,default=str)
    filename=f"golden-donut-diagnostics-{datetime.now():%Y%m%d-%H%M%S}.json"
    return Response(body,media_type="application/json; charset=utf-8",headers={"Content-Disposition":f'attachment; filename="{filename}"'})


@app.get("/analysis/{job_id}/download")
def download_analysis(job_id: int):
    connection = connect()
    try:
        row = connection.execute("SELECT output_path FROM analysis_jobs WHERE id=?",(job_id,)).fetchone()
    finally: connection.close()
    if not row or not row["output_path"]: raise HTTPException(404,"Результат ще не готовий")
    path = Path(row["output_path"])
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if ROOT.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404,"Файл результату недоступний")
    return FileResponse(path,filename=path.name)


class SourceUpdate(BaseModel):
    enabled: bool | None = None
    for_br: bool | None = None
    for_info: bool | None = None


class AnalysisRequest(BaseModel):
    job_type: str
    start: str
    end: str
    prompt: str = ""
    name: str = ""
    web: bool = False
    enhanced: bool = False
    priority: int = Field(100, ge=1, le=999)


class SourceCreateRequest(BaseModel):
    address: str
    title: str = ""
    audiences: str = ""
    for_br: bool = True
    for_info: bool = False
    save: bool = False


class EventEnrichmentRequest(BaseModel):
    event_ids: list[int] = Field(min_length=1, max_length=100)


class SourceCheckRequest(BaseModel):
    endpoint: str


class SourceEndpointReplaceRequest(BaseModel):
    old_endpoint: str
    address: str


@app.post("/api/originals/{document_id}/translate",dependencies=[Depends(require_action_token)])
def translate_original(document_id: int):
    connection=connect(False)
    try:
        document=connection.execute("SELECT id FROM source_documents WHERE id=?",(document_id,)).fetchone()
        if not document:
            raise HTTPException(404,"Оригінал не знайдено")
        translation_service.enqueue(connection, document_id)
    finally:
        connection.close()
    translation_service.kick_worker()
    return {"ok":True,"message":"Переклад поставлено в чергу"}


@app.get("/api/originals/{document_id}/translation")
def translation_status(document_id: int):
    connection=connect()
    try:
        row=connection.execute(
            "SELECT status,translated_text,error,updated_at FROM document_translations WHERE source_document_id=?",
            (document_id,),
        ).fetchone()
        return dict(row) if row else {"status":"none","translated_text":"","error":""}
    finally:
        connection.close()


@app.post("/api/process/{role}/{action}",dependencies=[Depends(require_action_token)])
def process_action(role: str, action: str):
    if role == "all":
        if action == "start": result=process_manager.start_all()
        elif action in {"stop","maintenance"}: result=process_manager.stop_all()
        else: raise HTTPException(400,"Невідома дія")
    else:
        if role not in process_manager.SPECS: raise HTTPException(404,"Невідомий процес")
        if action == "start": result=process_manager.start(role)
        elif action == "stop": result=process_manager.stop(role)
        elif action == "restart":
            process_manager.stop(role); result=process_manager.start(role)
        else: raise HTTPException(400,"Невідома дія")
    return {"ok":True,"processes":result}


@app.patch("/api/sources/{source_id}",dependencies=[Depends(require_action_token)])
def update_source(source_id: int, payload: SourceUpdate):
    fields,params=[],[]
    for key,value in payload.model_dump(exclude_none=True).items():
        fields.append(f"{key}=?"); params.append(value)
    if not fields: raise HTTPException(400,"Немає змін")
    connection=connect(False)
    try:
        result=connection.execute(f"UPDATE sources SET {','.join(fields)} WHERE id=? RETURNING id",tuple([*params,source_id])).fetchone()
        if not result: raise HTTPException(404,"Джерело не знайдено")
        connection.commit(); return {"ok":True}
    finally: connection.close()


@app.post("/api/sources/verify",dependencies=[Depends(require_action_token)])
def verify_source(payload: SourceCreateRequest):
    try:
        verified = source_admin.verify(payload.address)
    except Exception as error:
        raise HTTPException(400, f"Перевірка не пройдена: {error}") from error
    source_id = None
    if payload.save:
        connection = connect(False)
        try:
            source_id = source_admin.save(
                connection, verified, title=payload.title,
                audiences=payload.audiences.split(","),
                for_br=payload.for_br, for_info=payload.for_info,
            )
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        finally:
            connection.close()
    return {
        "ok": True, "verified": verified, "source_id": source_id,
        "message": ("Джерело перевірено й додано" if payload.save else verified["message"]),
    }


@app.post("/api/sources/check-all",dependencies=[Depends(require_action_token)])
def start_source_check_all():
    state_path=ROOT / "runtime" / "source_health_check_state.json"
    if state_path.exists():
        try:
            state=json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("status")=="running":
                return {"ok":True,"already_running":True,"message":"Перевірка вже виконується","state":state}
        except Exception:
            pass
    script=ROOT / "scripts" / "source_health_check_all.py"
    creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0)
    subprocess.Popen(
        [sys.executable,str(script)],cwd=str(ROOT),
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    return {"ok":True,"message":"Масову перевірку джерел запущено"}


@app.get("/api/sources/check-all/status")
def source_check_all_status():
    state_path=ROOT / "runtime" / "source_health_check_state.json"
    if not state_path.exists():
        return {"status":"idle","total":0,"done":0,"ok":0,"failed":0,"missing_total":0}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as error:
        return {"status":"error","last_error":str(error),"total":0,"done":0,"ok":0,"failed":0,"missing_total":0}


@app.get("/api/sources/endpoints")
def source_endpoints_for_check():
    connection=connect()
    try:
        rows=connection.execute("""
            SELECT e.endpoint,e.kind,e.source_id,s.title
              FROM source_endpoints e
              JOIN sources s ON s.id=e.source_id
             WHERE e.enabled AND s.enabled
             ORDER BY s.title,e.endpoint
        """).fetchall()
        return {"ok":True,"count":len(rows),"items":[dict(row) for row in rows]}
    finally:
        connection.close()


@app.post("/api/sources/{source_id}/endpoint",dependencies=[Depends(require_action_token)])
def replace_source_endpoint(source_id: int, payload: SourceEndpointReplaceRequest):
    try:
        verified=source_admin.verify(payload.address)
    except Exception as error:
        raise HTTPException(400,f"Нова адреса не пройшла перевірку: {error}") from error
    connection=connect(False)
    try:
        source=connection.execute("SELECT id FROM sources WHERE id=?",(source_id,)).fetchone()
        if not source:
            raise HTTPException(404,"Джерело не знайдено")
        old=connection.execute(
            "SELECT endpoint FROM source_endpoints WHERE source_id=? AND endpoint=?",
            (source_id,payload.old_endpoint),
        ).fetchone()
        if not old:
            raise HTTPException(404,"Стару точку джерела не знайдено")
        new_endpoint=verified["endpoint"]
        existing=connection.execute(
            "SELECT source_id FROM source_endpoints WHERE endpoint=?",(new_endpoint,)
        ).fetchone()
        if existing and int(existing["source_id"])!=source_id:
            raise HTTPException(409,"Ця точка вже належить іншому джерелу")
        connection.execute("DELETE FROM source_health WHERE endpoint=?",(payload.old_endpoint,))
        if new_endpoint!=payload.old_endpoint and existing:
            connection.execute(
                "DELETE FROM source_endpoints WHERE source_id=? AND endpoint=?",
                (source_id,payload.old_endpoint),
            )
            connection.execute(
                "UPDATE source_endpoints SET kind=?,enabled=true WHERE endpoint=?",
                (verified["endpoint_kind"],new_endpoint),
            )
        else:
            connection.execute(
                """UPDATE source_endpoints SET endpoint=?,kind=?,enabled=true
                     WHERE source_id=? AND endpoint=?""",
                (new_endpoint,verified["endpoint_kind"],source_id,payload.old_endpoint),
            )
        connection.execute(
            "UPDATE sources SET url=?,kind=?,enabled=true WHERE id=?",
            (verified["url"],verified["kind"],source_id),
        )
        connection.commit()
        missing=source_admin.store_check(connection,new_endpoint,verified)
        return {
            "ok":True,"endpoint":new_endpoint,"missing":missing,
            "message":f"Точку джерела оновлено: {new_endpoint}",
        }
    except HTTPException:
        connection.rollback()
        raise
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@app.post("/api/sources/check",dependencies=[Depends(require_action_token)])
def check_existing_source(payload: SourceCheckRequest):
    connection=connect(False)
    try:
        endpoint=connection.execute("SELECT endpoint FROM source_endpoints WHERE endpoint=?",(payload.endpoint,)).fetchone()
        if not endpoint:
            raise HTTPException(404,"Точку джерела не знайдено")
        try:
            verified=source_admin.verify(payload.endpoint)
            missing=source_admin.store_check(connection,payload.endpoint,verified)
            message=verified["message"]
            if missing:
                message += f"; у базі може бракувати повідомлень: {missing}"
            return {"ok":True,"message":message,"missing":missing}
        except Exception as error:
            source_admin.store_check(connection,payload.endpoint,error=str(error))
            raise HTTPException(400,f"Джерело не відповіло: {error}") from error
    finally:
        connection.close()


@app.post("/api/logs/{role}/clear",dependencies=[Depends(require_action_token)])
def clear_log_view(role: str):
    if role not in process_manager.SPECS:
        raise HTTPException(404,"Невідомий журнал")
    process_manager.clear_view(role)
    return {"ok":True,"message":"Поле журналу очищено; діагностична історія збережена"}


@app.post("/api/queue/{kind}/{row_id}/{action}",dependencies=[Depends(require_action_token)])
def queue_action(kind: str,row_id: int,action: str):
    table={"notes":"note_drafts","events":"event_candidates"}.get(kind)
    if not table: raise HTTPException(404,"Невідома черга")
    target={"requeue":"queued","reject":"rejected"}.get(action)
    if not target: raise HTTPException(400,"Невідома дія")
    connection=connect(False)
    try:
        row=connection.execute(f"SELECT status FROM {table} WHERE id=?",(row_id,)).fetchone()
        if not row: raise HTTPException(404,"Запис не знайдено")
        if row["status"] == "processing": raise HTTPException(409,"Запис зараз обробляється")
        connection.execute(f"UPDATE {table} SET status=?,error='',locked_at=NULL,completed_at=NULL WHERE id=?",(target,row_id))
        connection.commit(); return {"ok":True}
    finally: connection.close()


@app.post("/api/queue/collector/{document_id}/{route}/requeue",dependencies=[Depends(require_action_token)])
def requeue_collector_original(document_id: int, route: str):
    if route not in {"br", "info"}:
        raise HTTPException(404,"Невідомий маршрут")
    connection=connect(False)
    try:
        row=connection.execute(
            f"""UPDATE source_documents SET {route}_attempts=0,
                       {route}_next_retry_at=NULL,{route}_last_error=''
                   WHERE id=? RETURNING id""", (document_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404,"Оригінал не знайдено")
        connection.commit()
    finally:
        connection.close()
    process_manager.start("collector")
    return {"ok":True,"message":f"Оригінал #{document_id} повернуто збирачу"}


@app.post("/api/queue/notes/requeue-manual",dependencies=[Depends(require_action_token)])
def requeue_manual_notes():
    connection=connect(False)
    try:
        rows=connection.execute(
            """UPDATE note_drafts SET status='queued',attempts=0,error='',locked_at=NULL,completed_at=NULL
               WHERE status='manual_review' RETURNING id"""
        ).fetchall()
        connection.commit()
    finally:
        connection.close()
    process_manager.start("editor")
    return {"ok":True,"changed":len(rows),"message":f"Повернуто в обробку: {len(rows)}"}


@app.post("/api/queue/events/requeue-manual",dependencies=[Depends(require_action_token)])
def requeue_manual_events():
    connection=connect(False)
    try:
        rows=connection.execute(
            """UPDATE event_candidates SET status='queued',attempts=0,error='',
               locked_at=NULL,completed_at=NULL
               WHERE status IN ('manual_review','failed') RETURNING id"""
        ).fetchall()
        connection.commit()
    finally:
        connection.close()
    process_manager.start("analyst")
    return {"ok":True,"changed":len(rows),"message":f"Передано аналітику подій: {len(rows)}"}


@app.post("/api/calendar/{event_id}/status/{status}",dependencies=[Depends(require_action_token)])
def event_status(event_id:int,status:str):
    if status not in {"tentative","confirmed","postponed","cancelled","completed"}:
        raise HTTPException(400,"Невідомий статус")
    connection=connect(False)
    try:
        row=connection.execute("UPDATE calendar_events SET status=?,updated_at=now() WHERE id=? RETURNING id",(status,event_id)).fetchone()
        if not row: raise HTTPException(404,"Подію не знайдено")
        connection.commit(); return {"ok":True}
    finally: connection.close()


@app.post("/api/calendar/backfill-dates", dependencies=[Depends(require_action_token)])
def backfill_calendar_dates():
    connection = connect(False)
    try:
        changed = calendar_dates.apply_unknown_dates(connection)
        return {"ok": True, "changed": len(changed)}
    finally:
        connection.close()


@app.post("/api/calendar/enrich", dependencies=[Depends(require_action_token)])
def enrich_calendar_events(payload: EventEnrichmentRequest):
    connection = connect(False)
    queued = 0
    try:
        for event_id in dict.fromkeys(payload.event_ids):
            event = connection.execute(
                "SELECT * FROM calendar_events WHERE id=?", (event_id,)
            ).fetchone()
            if not event:
                continue
            already = connection.execute(
                """SELECT id FROM event_candidates
                   WHERE payload->>'manual_enrichment'='true'
                     AND payload->>'event_id'=?
                     AND status IN ('queued','processing') LIMIT 1""", (str(event_id),)
            ).fetchone()
            if already:
                continue
            candidate_text = (
                f"Уточнити картку події: {event['title']}. {event['summary']}"
            ).strip()
            connection.execute(
                """INSERT INTO event_candidates(
                     source_document_id,note_draft_id,candidate_key,candidate_text,payload)
                   VALUES (NULL,NULL,?,?,?::jsonb)""",
                (event["canonical_key"], candidate_text, json.dumps({
                    "manual_enrichment": True, "event_id": event_id, "source_url": "",
                }, ensure_ascii=False)),
            )
            queued += 1
        connection.commit()
    finally:
        connection.close()
    process_manager.start("analyst")
    return {"ok":True,"changed":queued,"message":f"Передано аналітику подій: {queued}"}


@app.post("/api/analysis",dependencies=[Depends(require_action_token)])
def submit_analysis(payload:AnalysisRequest):
    if payload.job_type not in {"daily","weekly","research","calendar"}:
        raise HTTPException(400,"Невідомий тип аналізу")
    if payload.enhanced and payload.job_type != "research":
        raise HTTPException(400,"Astra доступна лише для окремого тематичного дослідження")
    if payload.job_type=="research" and not payload.prompt.strip():
        raise HTTPException(400,"Опишіть аналітичний запит")
    start=analyst.local_time(payload.start); end=analyst.local_time(payload.end)
    if end<=start: raise HTTPException(400,"Кінець періоду має бути пізніше початку")
    connection=connect(False)
    try:
        job_id=workflow.submit_analysis_job(
            connection,payload.job_type,payload.prompt,start,end,
            {"web":payload.web,"enhanced":payload.enhanced,"name":payload.name},payload.priority,
        )
    finally: connection.close()
    process_manager.start("analyst")
    return {"ok":True,"job_id":job_id}


@app.post("/api/analysis/{job_id}/cancel",dependencies=[Depends(require_action_token)])
def cancel_analysis(job_id:int):
    connection=connect(False)
    try:
        row=connection.execute(
            "UPDATE analysis_jobs SET status='cancelled',completed_at=now() WHERE id=? AND status='queued' RETURNING id",
            (job_id,),
        ).fetchone()
        if not row: raise HTTPException(409,"Можна скасувати лише завдання в черзі")
        connection.commit(); return {"ok":True}
    finally: connection.close()


@app.post("/api/history/analysis/clear",dependencies=[Depends(require_action_token)])
def clear_analysis_history():
    connection=connect(False)
    paths=[]
    try:
        rows=connection.execute(
            "SELECT id,output_path FROM analysis_jobs WHERE status IN ('completed','failed','cancelled')"
        ).fetchall()
        for row in rows:
            if row["output_path"]:
                paths.append(Path(row["output_path"]))
        connection.execute(
            "DELETE FROM analysis_jobs WHERE status IN ('completed','failed','cancelled')"
        )
        connection.commit()
    finally:
        connection.close()
    for raw in paths:
        path=(raw if raw.is_absolute() else ROOT/raw).resolve()
        if ROOT.resolve() in path.parents and path.is_file():
            try:
                parent=path.parent
                reports=(ROOT/"analysis_reports").resolve()
                if reports in parent.parents and parent.name.startswith("job-"):
                    shutil.rmtree(parent)
                else:
                    path.unlink()
            except OSError:
                pass
    return {"ok":True,"changed":len(rows),"message":f"Історію завершених завдань очищено: {len(rows)}"}


@app.post("/api/analysis/{job_id}/retry",dependencies=[Depends(require_action_token)])
def retry_analysis(job_id:int):
    connection=connect(False)
    try:
        row=connection.execute(
            """UPDATE analysis_jobs SET status='queued',started_at=NULL,completed_at=NULL,
               output_path='',result='{}'::jsonb,error=''
               WHERE id=? AND status IN ('failed','cancelled') RETURNING id""", (job_id,)
        ).fetchone()
        if not row: raise HTTPException(409,"Повторити можна лише невдале або скасоване завдання")
        connection.commit()
    finally: connection.close()
    process_manager.start("analyst")
    return {"ok":True,"message":f"Завдання #{job_id} повернуто в чергу"}


def main():
    host = os.getenv("WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.getenv("WEB_PORT", "8000"))
    if os.getenv("WEB_OPEN_BROWSER", "true").lower() in {"1", "true", "yes"}:
        browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        threading.Timer(1.2, lambda: webbrowser.open(f"http://{browser_host}:{port}")).start()
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)


if __name__=="__main__":
    main()
