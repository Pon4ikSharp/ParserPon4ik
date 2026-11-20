from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db, telegram_markers

HASHTAG_RE = re.compile(r"(?<!\w)#[\w\-]+", re.UNICODE)
QUOTE_RE = re.compile(r'«([^»\n]{3,120})»|“([^”\n]{3,120})”|"([^"\n]{3,120})"')
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
WORD_RE = re.compile(r"[^\W_][\w’'\-]{2,}", re.UNICODE)
SPACE_RE = re.compile(r"\s+")

STOPWORDS = {
    "это","этот","эта","эти","что","как","для","или","уже","все","всё","еще","ещё",
    "был","была","были","будет","будут","мы","вы","они","он","она","оно","не","на",
    "в","во","и","а","но","с","со","к","у","от","по","за","из","про","при","о","об",
    "до","над","под","же","то","так","тут","там","его","ее","её","их","наш","наша",
    "цей","ця","ці","це","що","як","для","або","вже","всі","ще","був","була","були",
    "буде","будуть","ми","ви","вони","він","вона","не","на","у","в","і","та","але",
    "з","із","до","від","по","за","про","при","над","під","його","її","їх",
    "the","this","that","with","from","for","and","but","are","was","were","will","have",
    "has","had","not","you","they","their","our","dans","avec","pour","les","des","une",
    "un","est","sur","que","qui","pas","par","aux","du","de","la","le","et",
    "oraz","jest","nie","dla","się","jak","na","do","od","przez","pod","nad",
}

BOILERPLATE_PARTS = (
    "подписаться", "підписатися", "читать далее", "читати далі", "наш канал",
    "прислать новость", "связь с редакцией", "больше новостей", "головні новини",
    "les principales nouvelles", "bonjour de russie", "follow us", "subscribe",
    "telegram", "rutube", "vk.com",
)

TARGET_STEMS = (
    "украин","україн","киев","київ","зеленск","тцк","всу","зсу","нато","запад",
    "захід","европ","європ","санкц","мобилизац","мобілізац","ukrain","zelensk",
    "kyiv","kiev","nato","otan",
)

NARRATIVE_STEMS = (
    "крах","коллапс","колапс","банкрот","дефолт","катастроф","развал","розвал",
    "экономагеддон","економагеддон","хунт","диктатур","нелегитим","нелегітим",
    "глобалист","марионет","маріонет","бусиф","могилиз","капитуляц","капітуляц",
    "репресс","цензур","пропаганд","черная зима","чёрная зима","чорна зима",
)


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower().replace("ё", "е")
    text = URL_RE.sub(" ", text)
    return SPACE_RE.sub(" ", text).strip()


def clean_term(text: str) -> str:
    text = norm(text)
    text = text.strip(" .,:;!?—–-()[]{}<>|/\\*_=+~'\"")
    return SPACE_RE.sub(" ", text).strip()


def useful_term(term: str, term_type: str) -> bool:
    if not term or len(term) < 4 or len(term) > 120:
        return False
    if any(part in term for part in BOILERPLATE_PARTS):
        return False
    words = WORD_RE.findall(term)
    if term_type != "hashtag":
        if not 2 <= len(words) <= 6:
            return False
        content = [w for w in words if w not in STOPWORDS]
        if not content:
            return False
        if max(map(len, content), default=0) < 4:
            return False
    return True


def extract_terms(text: str):
    raw = text or ""
    normalized = norm(raw)
    found = set()

    for marker_type, marker, normalized, _index in telegram_markers.extract_markers(raw):
        term_type = "hashtag" if marker_type == "hashtag" else "label"
        term = clean_term(marker if marker_type == "hashtag" else normalized)
        if useful_term(term, term_type):
            found.add((term_type, term))

    for match in QUOTE_RE.finditer(raw):
        phrase = next((x for x in match.groups() if x), "")
        term = clean_term(phrase)
        if useful_term(term, "quoted"):
            found.add(("quoted", term))

    tokens = [t for t in WORD_RE.findall(normalized) if len(t) <= 40][:320]
    for n in (2, 3):
        for i in range(0, max(0, len(tokens) - n + 1)):
            gram_tokens = tokens[i:i+n]
            if gram_tokens[0] in STOPWORDS or gram_tokens[-1] in STOPWORDS:
                continue
            term = " ".join(gram_tokens)
            if useful_term(term, "ngram"):
                found.add(("ngram", term))

    return found


def candidate_score(term_type, term, count_24h, count_7d, target_docs, channels, burst):
    score = {"hashtag": 4, "label": 3, "quoted": 2, "ngram": 0}.get(term_type, 0)
    score += min(5, count_7d // 2)
    if target_docs >= 2:
        score += 2
    if channels >= 2:
        score += 2
    if count_24h >= 2 and burst >= 2.5:
        score += 2
    if any(stem in term for stem in NARRATIVE_STEMS):
        score += 3
    return min(score, 20)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--max-per-channel", type=int, default=400)
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=max(1, args.days))
    since24 = now - timedelta(hours=24)

    connection = db.connect(readonly=False)
    try:
        rows = connection.execute(
            """SELECT id,source_id,source_title,
                      COALESCE(NULLIF(full_text,''),source_text,'') AS text,
                      COALESCE(NULLIF(published_at,'')::timestamptz,
                               NULLIF(created_at,'')::timestamptz) AS event_at
                 FROM source_documents
                WHERE source_kind='telegram'
                  AND COALESCE(NULLIF(created_at,'')::timestamptz,
                               NULLIF(published_at,'')::timestamptz) >= ?
                ORDER BY id""",
            (since,),
        ).fetchall()

        stats = {}
        term_channels = defaultdict(set)

        for row in rows:
            channel_key = str(row["source_id"] or row["source_title"] or "unknown")
            channel = stats.setdefault(channel_key, {
                "source_id": row["source_id"],
                "title": row["source_title"] or "",
                "terms": {},
            })
            body = norm(row["text"])
            is_target = any(stem in body for stem in TARGET_STEMS)
            for term_type, term in extract_terms(row["text"]):
                key = (term_type, term)
                item = channel["terms"].setdefault(key, {
                    "count_24h": 0,
                    "count_7d": 0,
                    "target_docs": 0,
                    "first": row["event_at"],
                    "last": row["event_at"],
                    "examples": [],
                })
                item["count_7d"] += 1
                if row["event_at"] >= since24:
                    item["count_24h"] += 1
                if is_target:
                    item["target_docs"] += 1
                item["first"] = min(item["first"], row["event_at"])
                item["last"] = max(item["last"], row["event_at"])
                if len(item["examples"]) < 5:
                    item["examples"].append(int(row["id"]))
                term_channels[key].add(channel_key)

        snapshot_at = now
        kept = 0
        channels_written = 0
        for channel_key, channel in stats.items():
            candidates = []
            for (term_type, term), item in channel["terms"].items():
                count7 = item["count_7d"]
                count24 = item["count_24h"]
                minimum = 1 if term_type == "hashtag" else 2 if term_type in {"label", "quoted"} else 4
                if count7 < minimum:
                    continue
                expected24 = max(0.35, count7 / max(1.0, args.days))
                burst = count24 / expected24 if expected24 else 0.0
                global_channels = len(term_channels[(term_type, term)])
                score = candidate_score(
                    term_type, term, count24, count7,
                    item["target_docs"], global_channels, burst,
                )
                if term_type == "ngram" and score < 3:
                    continue
                candidates.append((
                    score, count7, term_type, term, item, global_channels, burst
                ))

            candidates.sort(key=lambda x: (x[0], x[1], x[2] == "hashtag"), reverse=True)
            candidates = candidates[:max(20, args.max_per_channel)]
            if candidates:
                channels_written += 1

            for score, count7, term_type, term, item, global_channels, burst in candidates:
                connection.execute(
                    """INSERT INTO telegram_lexicon_terms_v1(
                           channel_key,source_id,channel_title,term_type,term,
                           count_24h,count_7d,target_docs_7d,global_channels_7d,
                           burst_score,candidate_score,first_seen_at,last_seen_at,
                           example_document_ids,snapshot_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?::jsonb,?,now())
                       ON CONFLICT(channel_key,term_type,term) DO UPDATE SET
                         source_id=excluded.source_id,
                         channel_title=excluded.channel_title,
                         count_24h=excluded.count_24h,
                         count_7d=excluded.count_7d,
                         target_docs_7d=excluded.target_docs_7d,
                         global_channels_7d=excluded.global_channels_7d,
                         burst_score=excluded.burst_score,
                         candidate_score=excluded.candidate_score,
                         first_seen_at=excluded.first_seen_at,
                         last_seen_at=excluded.last_seen_at,
                         example_document_ids=excluded.example_document_ids,
                         snapshot_at=excluded.snapshot_at,
                         updated_at=now()""",
                    (
                        channel_key, channel["source_id"], channel["title"],
                        term_type, term, item["count_24h"], count7,
                        item["target_docs"], global_channels, float(burst), int(score),
                        item["first"], item["last"],
                        json.dumps(item["examples"]), snapshot_at,
                    ),
                )
                kept += 1

        connection.execute(
            "DELETE FROM telegram_lexicon_terms_v1 WHERE snapshot_at < ? AND NOT promoted",
            (snapshot_at,),
        )
        connection.commit()
        print(json.dumps({
            "telegram_docs": len(rows),
            "channels_seen": len(stats),
            "channels_written": channels_written,
            "terms_kept": kept,
            "days": args.days,
        }, ensure_ascii=False))
    finally:
        connection.close()


if __name__ == "__main__":
    main()

