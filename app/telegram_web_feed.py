from __future__ import annotations

from datetime import datetime, timezone
import re

import httpx
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; GoldenDonut/1.0)"

def _dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        out = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=timezone.utc)
    return out.astimezone(timezone.utc)

def fetch_page(name: str, *, before: int | None = None, proxy: str | None = None) -> tuple[str, list[dict]]:
    clean = (name or "").strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,}", clean):
        raise ValueError("invalid Telegram public username")
    url = f"https://t.me/s/{clean}"
    if before:
        url += f"?before={int(before)}"
    with httpx.Client(proxy=proxy or None, timeout=25, follow_redirects=True, trust_env=False, verify=False, headers={"User-Agent": UA}) as client:
        response = client.get(url)
        response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    title_node = soup.select_one(".tgme_channel_info_header_title")
    title = title_node.get_text(" ", strip=True) if title_node else f"@{clean}"
    items = []
    for node in soup.select(".tgme_widget_message[data-post]"):
        post = (node.get("data-post") or "").strip()
        if "/" not in post:
            continue
        channel, raw_id = post.rsplit("/", 1)
        try:
            message_id = int(raw_id)
        except ValueError:
            continue
        time_node = node.select_one("time[datetime]")
        published_at = _dt(time_node.get("datetime", "") if time_node else "")
        text_node = node.select_one(".tgme_widget_message_text")
        text = text_node.get_text("\n", strip=True) if text_node else ""
        if not text:
            caption = node.select_one(".tgme_widget_message_caption")
            text = caption.get_text("\n", strip=True) if caption else ""
        items.append({"message_id": message_id, "channel": channel, "title": title, "text": text, "published_at": published_at, "url": f"https://t.me/{channel}/{message_id}"})
    items.sort(key=lambda x: x["message_id"])
    return title, items

def fetch_since(name: str, since: datetime, *, proxy: str | None = None, max_pages: int = 80) -> tuple[str, list[dict]]:
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    since = since.astimezone(timezone.utc)
    before = None; title = f"@{name.lstrip('@')}"; found = {}
    for _ in range(max_pages):
        title, page = fetch_page(name, before=before, proxy=proxy)
        if not page:
            break
        for item in page:
            if item["published_at"] is None or item["published_at"] >= since:
                found[item["message_id"]] = item
        dated = [x for x in page if x["published_at"] is not None]
        if dated and min(x["published_at"] for x in dated) < since:
            break
        earliest = min(x["message_id"] for x in page)
        if before == earliest or earliest <= 1:
            break
        before = earliest
    return title, [found[k] for k in sorted(found)]
