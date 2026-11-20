from __future__ import annotations

import gzip
import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

from app import primary_dedup

SUPPORTED_HOSTS = {
    "www.hlavnydennik.sk": "Hlavný Denník",
    "hlavnydennik.sk": "Hlavný Denník",
    "mlyn.by": "Минская правда",
    "www.mlyn.by": "Минская правда",
    "rt.rs": "RT Balkan",
    "www.rt.rs": "RT Balkan",
    "tsargrad.tv": "Царьград",
    "www.tsargrad.tv": "Царьград",
    "www.turkiyetoday.com": "T?rkiye Today",
    "turkiyetoday.com": "T?rkiye Today",
    "www.vesti.ru": "\u0412\u0435\u0441\u0442\u0438",
    "vesti.ru": "\u0412\u0435\u0441\u0442\u0438",
    "www.inosmi.ru": "\u0418\u043d\u043e\u0421\u041c\u0418",
    "inosmi.ru": "\u0418\u043d\u043e\u0421\u041c\u0418",
}


def supports(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in SUPPORTED_HOSTS


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _text(node: ET.Element, name: str) -> str:
    for child in node.iter():
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _get(client: httpx.Client, url: str) -> bytes:
    response = client.get(url)
    response.raise_for_status()
    data = response.content
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def _client(proxy: str | None = None) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": "BR-News-Monitor/1.0 (+sitemap reader)"},
        proxy=proxy or None, follow_redirects=True, timeout=25,
        trust_env=False, verify=False,
    )


def _mlyn_maps(client: httpx.Client) -> list[str]:
    root = ET.fromstring(_get(client, "https://mlyn.by/sitemap_index.xml"))
    numbered = []
    for node in root:
        loc = _text(node, "loc")
        match = re.search(r"/post-sitemap(\d+)\.xml$", loc)
        if match:
            numbered.append((int(match.group(1)), loc))
    numbered.sort(reverse=True)
    return [url for _, url in numbered[:2]]


def _tsargrad_maps(client: httpx.Client) -> list[str]:
    root = ET.fromstring(_get(client, "https://tsargrad.tv/sitemap"))
    dated = []
    for node in root:
        loc = _text(node, "loc")
        match = re.search(r"sitemap-(\d{4})-(\d{1,2})\.xml\.gz$", loc)
        if match:
            dated.append(((int(match.group(1)), int(match.group(2))), loc))
    dated.sort(reverse=True)
    return [dated[0][1]] if dated else []


def _inosmi_maps(client: httpx.Client) -> list[str]:
    root = ET.fromstring(_get(client, "https://inosmi.ru/sitemap_article_index.xml"))
    candidates = []
    for node in root:
        loc = _text(node, "loc")
        lastmod = _text(node, "lastmod")
        if loc and "sitemap_article.xml" in loc:
            candidates.append((lastmod, loc))
    candidates.sort(reverse=True)
    return [candidates[0][1]] if candidates else []


def _sitemaps_for(client: httpx.Client, endpoint: str) -> list[str]:
    host = (urlparse(endpoint).hostname or "").lower()
    if "hlavnydennik.sk" in host:
        return ["https://www.hlavnydennik.sk/sitemaps/news.xml"]
    if host.endswith("mlyn.by"):
        return _mlyn_maps(client)
    if host.endswith("rt.rs"):
        return ["https://rt.rs/newssitemap.xml"]
    if host.endswith("tsargrad.tv"):
        return _tsargrad_maps(client)
    if host.endswith("turkiyetoday.com"):
        return ["https://www.turkiyetoday.com/sitemap/google-news-sitemap"]
    if host.endswith("vesti.ru"):
        return ["https://www.vesti.ru/sitemap-news.xml"]
    if host.endswith("inosmi.ru"):
        return _inosmi_maps(client)
    return []


def _slug_title(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1]
    slug = re.sub(r"_\d+$", "", slug)
    return re.sub(r"[-_]+", " ", slug).strip()


def _entries(root: ET.Element) -> list[dict]:
    output = []
    for node in root:
        if _local(node.tag) != "url":
            continue
        url = _text(node, "loc")
        if not url or "/app/uploads/" in url:
            continue
        title = _text(node, "title") or _slug_title(url)
        date_text = _text(node, "publication_date") or _text(node, "lastmod")
        published_at = _parse_date(date_text)
        if published_at and published_at > datetime.now(timezone.utc) + timedelta(hours=6):
            continue
        canonical = primary_dedup.canonicalize_url(url)
        if not canonical:
            continue
        entry_key = hashlib.sha256(f"url|{canonical}".encode("utf-8")).hexdigest()
        output.append({
            "entry_key": entry_key,
            "title": title,
            "url": url,
            "source_text": title,
            "published_at": published_at,
        })
    return output


def fetch(endpoint: str, proxy: str | None = None) -> tuple[str, list[dict]]:
    host = (urlparse(endpoint).hostname or "").lower()
    source_title = SUPPORTED_HOSTS.get(host, host or endpoint)
    with _client(proxy) as client:
        sitemap_urls = _sitemaps_for(client, endpoint)
        entries = []
        for sitemap_url in sitemap_urls:
            entries.extend(_entries(ET.fromstring(_get(client, sitemap_url))))
    dedup = {entry["entry_key"]: entry for entry in entries}
    return source_title, list(dedup.values())
