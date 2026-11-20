"""Safe local verification and registration of Telegram and RSS sources."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from app import articles, collector, sources


def normalize_address(value: str) -> str:
    value = " ".join((value or "").strip().split())
    if not value:
        raise ValueError("Вставте адресу джерела")
    if value.startswith("@"):
        return value
    if "://" not in value and value.lower().startswith(("t.me/", "telegram.me/")):
        return "https://" + value
    if "://" not in value:
        return "https://" + value
    return value


def _telegram_result(address: str) -> dict:
    name = sources.telegram_name(address)
    if not re.fullmatch(r"[A-Za-z0-9_]{5,}", name):
        raise ValueError("Некоректна публічна адреса Telegram-каналу")
    if sources.excluded(name):
        raise ValueError("Це джерело виключене з моніторингу")
    public_url = f"https://t.me/s/{name}"
    proxy, _ = collector.configure_proxy()
    with httpx.Client(proxy=proxy, timeout=20, follow_redirects=True, trust_env=False) as client:
        response = client.get(public_url, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
    if "tgme_channel_info" not in response.text and "tgme_widget_message" not in response.text:
        raise ValueError("Telegram не підтвердив існування публічного каналу")
    soup = BeautifulSoup(response.text, "html.parser")
    title_node = soup.select_one(".tgme_channel_info_header_title")
    title = title_node.get_text(" ", strip=True) if title_node else f"@{name}"
    latest = soup.select(".tgme_widget_message")
    latest_node = latest[-1] if latest else None
    latest_time = latest_node.select_one("time[datetime]") if latest_node else None
    latest_key = latest_node.get("data-post", "").split("/")[-1] if latest_node else ""
    return {
        "kind": "telegram", "endpoint_kind": "telegram", "endpoint": f"@{name}",
        "url": f"https://t.me/{name}", "title": title,
        "message": f"Telegram-канал @{name} доступний",
        "latest_at": latest_time.get("datetime", "") if latest_time else "",
        "latest_key": latest_key,
    }


def _rss_candidate(url: str) -> dict | None:
    previous_proxy = collector._PROXY_URL
    try:
        collector._PROXY_URL, _ = collector.configure_proxy()
        title, entries = collector.fetch_rss_feed(url)
    except Exception:
        return None
    finally:
        collector._PROXY_URL = previous_proxy
    latest = max(
        entries,
        key=lambda item: item.get("published_at") or datetime.min.replace(tzinfo=timezone.utc),
        default={},
    )
    return {
        "kind": "web", "endpoint_kind": "rss", "endpoint": url, "url": url,
        "title": title, "message": f"RSS/Atom працює, знайдено записів: {len(entries)}",
        "latest_at": latest.get("published_at").isoformat() if latest.get("published_at") else "",
        "latest_key": latest.get("entry_key", ""),
    }


def _web_result(address: str) -> dict:
    articles.public_url(address)
    direct = _rss_candidate(address)
    if direct:
        return direct
    proxy, _ = collector.configure_proxy()
    with httpx.Client(proxy=proxy, timeout=25, follow_redirects=True, trust_env=False) as client:
        response = client.get(address, headers={"User-Agent": "Mozilla/5.0 (compatible; GoldenDonut/1.0)"})
        response.raise_for_status()
        if len(response.content) > 5_000_000:
            raise ValueError("Сторінка перевищує безпечний розмір перевірки")
    final_url = str(response.url)
    soup = BeautifulSoup(response.text, "html.parser")
    candidates = []
    for link in soup.select('link[rel~="alternate"][href]'):
        media_type = (link.get("type") or "").lower()
        if media_type in {"application/rss+xml", "application/atom+xml", "application/xml", "text/xml"}:
            candidates.append(urljoin(final_url, link["href"]))
    root = f"{urlsplit(final_url).scheme}://{urlsplit(final_url).netloc}/"
    candidates.extend(urljoin(root, item) for item in ("feed/", "rss", "rss.xml", "feed.xml"))
    for candidate in dict.fromkeys(candidates):
        try:
            articles.public_url(candidate)
        except ValueError:
            continue
        result = _rss_candidate(candidate)
        if result:
            result["url"] = final_url
            result["message"] = f"Сайт доступний; RSS/Atom знайдено: {candidate}"
            return result
    raise ValueError(
        "Сайт доступний, але RSS/Atom не знайдено. Без RSS автоматичний збір із цього сайту поки неможливий"
    )


def verify(address: str) -> dict:
    normalized = normalize_address(address)
    if normalized.startswith("@") or urlsplit(normalized).hostname in {"t.me", "telegram.me"}:
        return _telegram_result(normalized)
    return _web_result(normalized)


def save(connection, verified: dict, *, title: str = "", audiences=None,
         for_br: bool = True, for_info: bool = False) -> int:
    if not for_br and not for_info:
        raise ValueError("Позначте використання для БР або інфовпливу")
    for_br = bool(for_br or for_info)
    audiences = list(dict.fromkeys(item.strip() for item in (audiences or []) if item.strip()))
    endpoint = verified["endpoint"]
    existing_endpoint = connection.execute(
        "SELECT source_id FROM source_endpoints WHERE endpoint=?", (endpoint,)
    ).fetchone()
    key = sources.source_key(verified["url"])
    source = (
        connection.execute("SELECT * FROM sources WHERE id=?", (existing_endpoint["source_id"],)).fetchone()
        if existing_endpoint else
        connection.execute("SELECT * FROM sources WHERE source_key=?", (key,)).fetchone()
    )
    source_title = (title or verified["title"] or verified["url"]).strip()[:300]
    if source:
        combined_audiences = list(dict.fromkeys([*(source["audiences"] or []), *audiences]))
        connection.execute(
            """UPDATE sources SET title=?,url=?,kind=?,audiences=?::jsonb,for_br=?,for_info=?,
               enabled=true,audience_origin='manual' WHERE id=?""",
            (source_title, verified["url"], verified["kind"],
             json.dumps(combined_audiences, ensure_ascii=False),
             bool(source["for_br"] or for_br), bool(source["for_info"] or for_info), source["id"]),
        )
        source_id = source["id"]
    else:
        source_id = connection.execute(
            """INSERT INTO sources(source_key,kind,title,url,audiences,for_br,for_info,enabled,audience_origin)
               VALUES (?,?,?,?,?::jsonb,?,?,true,'manual') RETURNING id""",
            (key, verified["kind"], source_title, verified["url"],
             json.dumps(audiences, ensure_ascii=False), for_br, for_info),
        ).fetchone()["id"]
    connection.execute(
        """INSERT INTO source_endpoints(endpoint,source_id,kind,enabled) VALUES (?,?,?,true)
           ON CONFLICT(endpoint) DO UPDATE SET source_id=excluded.source_id,kind=excluded.kind,enabled=true""",
        (endpoint, source_id, verified["endpoint_kind"]),
    )
    connection.commit()
    return source_id


def store_check(connection, endpoint: str, result=None, error=""):
    result = result or {}
    remote_key = str(result.get("latest_key") or "")
    database_key = ""
    missing = 0
    endpoint_row = connection.execute(
        "SELECT source_id FROM source_endpoints WHERE endpoint=?", (endpoint,)
    ).fetchone()
    if endpoint_row:
        document = connection.execute(
            """SELECT source_message_id,published_at FROM source_documents
               WHERE source_id=? ORDER BY NULLIF(published_at,'')::timestamptz DESC NULLS LAST,id DESC LIMIT 1""",
            (endpoint_row["source_id"],),
        ).fetchone()
        database_key = str(document["source_message_id"]) if document else ""
        if remote_key.isdigit() and database_key.lstrip("-").isdigit():
            missing = max(0, int(remote_key) - int(database_key))
    connection.execute(
        """INSERT INTO source_health(endpoint,checked_at,check_ok,remote_item_at,
                 remote_item_key,missing_count,error,details)
           VALUES (?,now(),?,NULLIF(?,'')::timestamptz,?,?,?,?::jsonb)
           ON CONFLICT(endpoint) DO UPDATE SET checked_at=now(),check_ok=excluded.check_ok,
             remote_item_at=excluded.remote_item_at,remote_item_key=excluded.remote_item_key,
             missing_count=excluded.missing_count,error=excluded.error,details=excluded.details""",
        (endpoint, not bool(error), result.get("latest_at", ""), remote_key, missing,
         str(error)[:2000], json.dumps(result, ensure_ascii=False, default=str)),
    )
    connection.commit()
    return missing
