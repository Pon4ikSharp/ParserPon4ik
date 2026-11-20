from __future__ import annotations

import html as html_lib
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from app import db, info_prefilter, primary_dedup

SITEMAP_INDEX = "https://dzen.ru/sitemaps/news-runtime/sitemap.xml"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/153.0.0.0 Safari/537.36"
)
STORY_ID_RE = re.compile(r"/news/story/([0-9a-f-]{36})", re.I)
BOOTSTRAP_RE = re.compile(r"var it = (\{.*?\});", re.S)
CONTAINER_RE = re.compile(r"element2\.value = '([^']+)'", re.S)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _xml_text(node: ET.Element, local_name: str) -> str:
    for child in node.iter():
        if child.tag.rsplit("}", 1)[-1] == local_name:
            return (child.text or "").strip()
    return ""


def _story_id(url: str) -> str:
    match = STORY_ID_RE.search(url or "")
    return match.group(1).lower() if match else ""


def _discovery_score(title: str, source_name: str = "") -> tuple[int, list[str]]:
    score, hits = info_prefilter.domain_score(title, title)
    actor_hits = info_prefilter.actor_mention_hits(title, title)
    if actor_hits:
        score = min(20, score + min(6, len(actor_hits) * 2))
    source_prior, prior_label = info_prefilter.source_influence_prior(source_name)
    if source_prior:
        score = min(20, score + source_prior)
    labels = list(dict.fromkeys([*hits, *actor_hits, prior_label] if prior_label else [*hits, *actor_hits]))
    return score, labels


def _canonical_url(value: str) -> str:
    return primary_dedup.canonicalize_url(value or "") or (value or "")


class DzenClient:
    def __init__(self, timeout: int = 25):
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"},
            follow_redirects=True,
            verify=False,
            timeout=timeout,
            trust_env=False,
        )

    def close(self) -> None:
        self.client.close()

    def _install_sso(self, bootstrap_html: str) -> str:
        match = BOOTSTRAP_RE.search(bootstrap_html)
        container = CONTAINER_RE.search(bootstrap_html)
        if not match or not container:
            raise RuntimeError("Dzen SSO bootstrap не розпізнано")
        payload = json.loads(match.group(1))
        retpath = html_lib.unescape(payload["retpath"])
        self.client.post(
            payload["host"],
            data={"retpath": retpath, "container": container.group(1), "dzen": "1"},
        ).raise_for_status()
        return retpath

    def get_story_html(self, story_url: str) -> str:
        response = self.client.get(story_url)
        response.raise_for_status()
        if "window.Ya.Neo" in response.text:
            return response.text
        retpath = self._install_sso(response.text)
        response = self.client.get(retpath)
        response.raise_for_status()
        if "window.Ya.Neo" not in response.text:
            raise RuntimeError("Dzen Story не віддав server JSON після SSO")
        return response.text


def parse_neo_story(page_html: str) -> dict:
    marker = "window.Ya.Neo={"
    start = page_html.find(marker)
    if start < 0:
        raise ValueError("window.Ya.Neo не знайдено")
    start += len("window.Ya.Neo=")
    neo, _ = json.JSONDecoder().raw_decode(page_html[start:])
    story = (((neo.get("dataSource") or {}).get("news") or {}).get("story") or {})
    if not story.get("clusterId"):
        raise ValueError("Dzen Story JSON не містить clusterId")
    return story


def fetch_runtime_sitemap(client: DzenClient) -> list[dict]:
    index = client.client.get(SITEMAP_INDEX)
    index.raise_for_status()
    root = ET.fromstring(index.content)
    sitemap_url = _xml_text(root, "loc")
    if not sitemap_url:
        raise RuntimeError("Dzen runtime sitemap index порожній")
    response = client.client.get(sitemap_url)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    rows = []
    for node in root:
        url = _xml_text(node, "loc")
        cluster_id = _story_id(url)
        if not cluster_id:
            continue
        title = _xml_text(node, "title")
        source_name = _xml_text(node, "name")
        published = _parse_datetime(_xml_text(node, "publication_date"))
        score, hits = _discovery_score(title, source_name)
        rows.append({"cluster_id": cluster_id, "story_url": url, "title": title,
                     "source_name": source_name, "publication_at": published,
                     "discovery_score": score, "discovery_hits": hits})
    return rows


def upsert_discovery(connection, rows: list[dict]) -> int:
    saved = 0
    for row in rows:
        connection.execute(
            """INSERT INTO dzen_stories_v1(
                 cluster_id,story_url,title,representative_source,publication_at,
                 discovery_score,discovery_hits,last_seen_at)
               VALUES(?,?,?,?,?,?,?::jsonb,now())
               ON CONFLICT(cluster_id) DO UPDATE SET
                 story_url=excluded.story_url,
                 title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE dzen_stories_v1.title END,
                 representative_source=CASE WHEN excluded.representative_source<>'' THEN excluded.representative_source ELSE dzen_stories_v1.representative_source END,
                 publication_at=COALESCE(excluded.publication_at,dzen_stories_v1.publication_at),
                 discovery_score=GREATEST(dzen_stories_v1.discovery_score,excluded.discovery_score),
                 discovery_hits=excluded.discovery_hits,last_seen_at=now()""",
            (row["cluster_id"], row["story_url"], row["title"], row["source_name"],
             row["publication_at"], row["discovery_score"], json.dumps(row["discovery_hits"], ensure_ascii=False)),
        )
        saved += 1
    connection.commit()
    return saved


def _source_row(cluster_id: str, item: dict, *, representative: bool = False) -> dict:
    stats = item.get("studioDocumentStats") or {}
    published = _parse_datetime(str(stats.get("docPubDate") or ""))
    if published is None and stats.get("docPubDate"):
        published = datetime.fromtimestamp(int(stats["docPubDate"]), tz=timezone.utc)
    return {"cluster_id": cluster_id, "source_doc_id": str(item.get("docId") or item.get("sourceDocId") or ""),
            "agency_id": str(item.get("agencyId") or ""), "source_name": item.get("sourceName") or "",
            "title": item.get("title") or stats.get("docTitle") or "", "article_url": _canonical_url(stats.get("docUrl") or item.get("url") or ""),
            "published_at": published, "is_origin": bool(item.get("isOrigin")),
            "is_representative": representative, "raw_meta": item}


def upsert_story(connection, story: dict, *, raw_meta: dict | None = None) -> int:
    cluster_id = str(story.get("clusterId") or "")
    if not cluster_id:
        raise ValueError("clusterId is required")
    representative = {
        "sourceDocId": story.get("sourceDocId"), "agencyId": story.get("agencyId"),
        "sourceName": story.get("sourceName"), "title": story.get("title"),
        "url": story.get("sourceUrl"), "isOrigin": bool(story.get("isTitleDocOrigin")),
        "studioDocumentStats": story.get("studioDocumentStats") or {},
    }
    sources = [_source_row(cluster_id, representative, representative=True)]
    seen = {sources[0]["source_doc_id"]} if sources[0]["source_doc_id"] else set()
    for item in story.get("tail") or []:
        row = _source_row(cluster_id, item)
        if not row["source_doc_id"] or row["source_doc_id"] in seen:
            continue
        seen.add(row["source_doc_id"])
        sources.append(row)
    time_raw = story.get("timeRaw")
    publication_at = datetime.fromtimestamp(int(time_raw) / 1000, tz=timezone.utc) if time_raw else None
    discovery_score, discovery_hits = _discovery_score(story.get("title") or "", story.get("sourceName") or "")
    connection.execute(
        """INSERT INTO dzen_stories_v1(cluster_id,story_url,persistent_id,title,summary,
             representative_source,representative_url,representative_doc_id,publication_at,rubric_id,
             importance,source_count,discovery_score,discovery_hits,last_seen_at,last_fetched_at,fetch_status,raw_meta)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?::jsonb,now(),now(),'fetched',?::jsonb)
           ON CONFLICT(cluster_id) DO UPDATE SET persistent_id=excluded.persistent_id,title=excluded.title,
             summary=excluded.summary,representative_source=excluded.representative_source,
             representative_url=excluded.representative_url,representative_doc_id=excluded.representative_doc_id,
             publication_at=COALESCE(excluded.publication_at,dzen_stories_v1.publication_at),rubric_id=excluded.rubric_id,
             importance=excluded.importance,source_count=excluded.source_count,
             discovery_score=GREATEST(dzen_stories_v1.discovery_score,excluded.discovery_score),
             discovery_hits=excluded.discovery_hits,last_seen_at=now(),last_fetched_at=now(),fetch_status='fetched',fetch_error='',raw_meta=excluded.raw_meta""",
        (cluster_id, story.get("shortUrl") or story.get("url") or "", str(story.get("persistentId") or ""),
         story.get("title") or "", story.get("text") or "", story.get("sourceName") or "",
         _canonical_url(story.get("sourceUrl") or ""), str(story.get("sourceDocId") or ""), publication_at,
         str(story.get("rubricId") or ""), int((story.get("stat") or {}).get("importance") or 0), len(sources),
         discovery_score, json.dumps(discovery_hits,ensure_ascii=False), json.dumps(raw_meta or story,ensure_ascii=False,default=str)),
    )
    for row in sources:
        connection.execute(
            """INSERT INTO dzen_story_sources_v1(cluster_id,source_doc_id,agency_id,source_name,title,
                 article_url,published_at,is_origin,is_representative,last_seen_at,raw_meta)
               VALUES(?,?,?,?,?,?,?,?,?,now(),?::jsonb)
               ON CONFLICT(cluster_id,source_doc_id) DO UPDATE SET agency_id=excluded.agency_id,
                 source_name=excluded.source_name,title=excluded.title,article_url=excluded.article_url,
                 published_at=COALESCE(excluded.published_at,dzen_story_sources_v1.published_at),
                 is_origin=excluded.is_origin,is_representative=excluded.is_representative,
                 last_seen_at=now(),raw_meta=excluded.raw_meta""",
            (cluster_id,row["source_doc_id"],row["agency_id"],row["source_name"],row["title"],
             row["article_url"],row["published_at"],row["is_origin"],row["is_representative"],
             json.dumps(row["raw_meta"],ensure_ascii=False,default=str)),
        )
    connection.execute("UPDATE dzen_stories_v1 SET source_count=? WHERE cluster_id=?",(len(sources),cluster_id))
    return len(sources)


def fetch_and_store_story(connection, client: DzenClient, story_url: str) -> dict:
    cluster_id = _story_id(story_url)
    try:
        page_html = client.get_story_html(story_url)
        story = parse_neo_story(page_html)
        source_count = upsert_story(connection, story)
        connection.commit()
        return {"cluster_id": cluster_id, "ok": True, "source_count": source_count,
                "title": story.get("title") or ""}
    except Exception as exc:
        connection.rollback()
        if cluster_id:
            connection.execute(
                "UPDATE dzen_stories_v1 SET fetch_status='error',fetch_error=?,last_fetched_at=now() WHERE cluster_id=?",
                (str(exc)[:1200],cluster_id),
            )
            connection.commit()
        return {"cluster_id": cluster_id, "ok": False, "error": str(exc)}


LIVE_RUBRICS = (
    "https://dzen.ru/news/rubric/politics",
    "https://dzen.ru/news/rubric/svo",
    "https://dzen.ru/news/rubric/world",
    "https://dzen.ru/news/rubric/business",
    "https://dzen.ru/news/rubric/chronologic",
)


def parse_neo(page_html: str) -> dict:
    marker = "window.Ya.Neo={"
    start = page_html.find(marker)
    if start < 0:
        raise ValueError("window.Ya.Neo JSON не знайдено")
    start += len("window.Ya.Neo=")
    neo, _ = json.JSONDecoder().raw_decode(page_html[start:])
    return neo


def discover_live(client: DzenClient, urls: tuple[str, ...] = LIVE_RUBRICS) -> list[dict]:
    merged: dict[str, dict] = {}
    for url in urls:
        neo = parse_neo(client.get_story_html(url))
        news = ((neo.get("dataSource") or {}).get("news") or {})
        items = list(news.get("feed") or [])
        opertop = news.get("opertop") or {}
        items.extend(opertop.get("stories") or [])
        for item in items:
            story_url = item.get("url") or ""
            cluster_id = _story_id(story_url)
            if not cluster_id:
                continue
            title = item.get("title") or ""
            annotation = item.get("annotation") or ""
            source_name = item.get("source") or item.get("sourceName") or ""
            score, hits = _discovery_score(f"{title} {annotation}", source_name)
            current = merged.get(cluster_id)
            row = {
                "cluster_id": cluster_id,
                "story_url": story_url.split("?", 1)[0],
                "title": title,
                "summary": annotation,
                "source_name": source_name,
                "publication_at": None,
                "discovery_score": score,
                "discovery_hits": hits,
                "representative_url": _canonical_url(item.get("titleUrl") or ""),
                "persistent_id": str(item.get("persistentId") or item.get("id") or ""),
                "rubric_url": url,
            }
            if current is None or score > current["discovery_score"]:
                merged[cluster_id] = row
    return list(merged.values())


def upsert_live_discovery(connection, rows: list[dict]) -> int:
    for row in rows:
        connection.execute(
            """INSERT INTO dzen_stories_v1(cluster_id,story_url,persistent_id,title,summary,
                 representative_source,representative_url,publication_at,discovery_score,discovery_hits,last_seen_at)
               VALUES(?,?,?,?,?,?,?,?,?,?::jsonb,now())
               ON CONFLICT(cluster_id) DO UPDATE SET story_url=excluded.story_url,
                 persistent_id=CASE WHEN excluded.persistent_id<>'' THEN excluded.persistent_id ELSE dzen_stories_v1.persistent_id END,
                 title=excluded.title,summary=excluded.summary,representative_source=excluded.representative_source,
                 representative_url=excluded.representative_url,
                 discovery_score=GREATEST(dzen_stories_v1.discovery_score,excluded.discovery_score),
                 discovery_hits=excluded.discovery_hits,last_seen_at=now()""",
            (row["cluster_id"],row["story_url"],row["persistent_id"],row["title"],row["summary"],
             row["source_name"],row["representative_url"],row["publication_at"],row["discovery_score"],
             json.dumps(row["discovery_hits"],ensure_ascii=False)),
        )
    connection.commit()
    return len(rows)


def run_live_discovery(*, fetch_score: int = 4, fetch_limit: int = 80) -> dict:
    client = DzenClient()
    connection = db.connect()
    try:
        rows = discover_live(client)
        upsert_live_discovery(connection, rows)
        candidates = sorted(
            (row for row in rows if row["discovery_score"] >= fetch_score),
            key=lambda row: (-row["discovery_score"], row["title"]),
        )[:max(0, fetch_limit)]
        results = []
        for row in candidates:
            results.append(fetch_and_store_story(connection, client, row["story_url"]))
        return {
            "discovered": len(rows), "candidate_count": sum(1 for row in rows if row["discovery_score"] >= fetch_score),
            "fetched": sum(1 for result in results if result.get("ok")),
            "errors": sum(1 for result in results if not result.get("ok")), "results": results,
        }
    finally:
        connection.close()
        client.close()
